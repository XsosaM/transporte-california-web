import json
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import Dispatch, Driver, Incident, Route, Shift, Stop, Vehicle


def _api_request(method, path, payload=None, use_django_key=True, timeout=4.0):
    base_url = getattr(settings, "TRANSPORTE_API_BASE_URL", "http://127.0.0.1:8080/api/v1").rstrip("/")
    url = f"{base_url}/{path.lstrip('/')}"
    api_key = (
        getattr(settings, "TRANSPORTE_DJANGO_API_KEY", "")
        if use_django_key
        else getattr(settings, "TRANSPORTE_APP_API_KEY", "")
    )
    headers = {
        "Accept": "application/json",
        "X-API-Key": api_key,
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            if not body:
                return True, None
            return True, json.loads(body)
    except Exception as exc:
        return False, str(exc)


def _resolver_ruta_api_id(route_obj):
    if not route_obj:
        return 1
    code = (route_obj.code or "").upper()
    mapping = {
        "A-IDA": 1,
        "A": 1,
        "1": 1,
        "C-IDA": 2,
        "C": 2,
        "2": 2,
        "A-VUELTA": 3,
        "3": 3,
        "C-VUELTA": 4,
        "4": 4,
    }
    return mapping.get(code, route_obj.pk if route_obj.pk in (1, 2, 3, 4) else 1)


def sincronizar_incidencia_con_api(incident):
    """Publica o retira automáticamente una incidencia de Django en transporte-api."""
    if not incident or not incident.pk:
        return False

    # Si es una incidencia originada desde el celular del conductor (marcada con [APP-ID:...])
    # y se resuelve en Django, también cerramos la alerta original en la API
    app_inc_id = _extraer_app_incidencia_id(incident.location)

    if incident.status == Incident.Status.RESOLVED:
        ok, _ = _api_request("DELETE", f"integraciones/django/incidencias/{incident.pk}")
        if app_inc_id:
            _api_request("DELETE", f"integraciones/django/incidencias/{app_inc_id}")
        return ok

    route_obj = None
    if incident.dispatch_id and incident.dispatch and incident.dispatch.shift_id:
        route_obj = incident.dispatch.shift.route
    else:
        route_obj = Route.objects.filter(active=True).first()

    ruta_id = _resolver_ruta_api_id(route_obj)
    ahora = timezone.now()
    vigente_desde = (incident.created_at or ahora).isoformat()
    vigente_hasta = (ahora + timedelta(hours=8)).isoformat()

    ruta_label = f"Ruta {route_obj.code}" if route_obj else "Ruta General"
    titulo = f"{incident.get_category_display()} · {ruta_label}"
    if incident.dispatch_id and incident.dispatch.shift and incident.dispatch.shift.vehicle:
        titulo += f" (Unidad {incident.dispatch.shift.vehicle.plate})"

    payload = {
        "rutaId": ruta_id,
        "categoria": incident.category or "other",
        "severidad": incident.severity or "medium",
        "titulo": titulo[:150],
        "descripcion": incident.description or titulo,
        "ubicacion": (incident.location or "Trujillo")[:200],
        "vigenteDesde": vigente_desde,
        "vigenteHasta": vigente_hasta,
        "activa": True,
    }
    target_id = app_inc_id if app_inc_id else incident.pk
    ok, _ = _api_request("PUT", f"integraciones/django/incidencias/{target_id}", payload=payload)
    return ok


def retirar_incidencia_de_api(incident_pk, location=""):
    app_inc_id = _extraer_app_incidencia_id(location)
    if app_inc_id:
        _api_request("DELETE", f"integraciones/django/incidencias/{app_inc_id}")
    ok, _ = _api_request("DELETE", f"integraciones/django/incidencias/{incident_pk}")
    return ok


def _extraer_app_incidencia_id(location):
    if not location or "[APP-" not in location:
        return None
    try:
        start = location.index("[APP-") + 5
        end = location.index("]", start)
        return int(location[start:end])
    except Exception:
        return None


def sincronizar_unidad_con_api(vehicle):
    if not vehicle:
        return False
    payload = {
        "unitCode": vehicle.unit_code,
        "plate": vehicle.plate,
        "capacity": vehicle.capacity,
        "soatExpiry": vehicle.soat_expiry.isoformat() if vehicle.soat_expiry else None,
        "technicalReviewExpiry": vehicle.technical_review_expiry.isoformat() if vehicle.technical_review_expiry else None,
        "status": vehicle.status,
    }
    ok, _ = _api_request("POST", "integraciones/django/unidades", payload=payload)
    return ok


def sincronizar_conductor_con_api(driver):
    if not driver:
        return False
    payload = {
        "documentNumber": driver.document_number,
        "fullName": driver.full_name,
        "phone": driver.phone,
        "licenseNumber": driver.license_number,
        "licenseExpiry": driver.license_expiry.isoformat() if driver.license_expiry else None,
        "status": driver.status,
    }
    ok, _ = _api_request("POST", "integraciones/django/conductores", payload=payload)
    return ok


def sincronizar_turno_con_api(shift, dispatch=None):
    if not shift:
        return False
    dep_planned = shift.planned_departure
    est_arrival = (
        (datetime.combine(date.today(), dep_planned) + timedelta(minutes=55)).time()
        if dep_planned
        else time(8, 0)
    )
    status = shift.status
    actual_dep = None
    actual_arr = None
    if dispatch:
        status = dispatch.status
        if dispatch.departure_at:
            actual_dep = timezone.localtime(dispatch.departure_at).strftime("%H:%M:%S")
        if dispatch.arrival_at:
            actual_arr = timezone.localtime(dispatch.arrival_at).strftime("%H:%M:%S")

    payload = {
        "serviceDate": shift.service_date.isoformat() if shift.service_date else date.today().isoformat(),
        "routeCode": shift.route.code if shift.route_id else "A-IDA",
        "rutaId": _resolver_ruta_api_id(shift.route if shift.route_id else None),
        "driverDni": shift.driver.document_number if shift.driver_id else None,
        "vehiclePlate": shift.vehicle.plate if shift.vehicle_id else None,
        "plannedDeparture": dep_planned.strftime("%H:%M:%S") if dep_planned else "07:00:00",
        "estimatedArrival": est_arrival.strftime("%H:%M:%S"),
        "actualDeparture": actual_dep,
        "actualArrival": actual_arr,
        "status": status,
    }
    ok, _ = _api_request("POST", "integraciones/django/turnos", payload=payload)
    return ok


def _normalizar_posiciones(posiciones):
    normalizadas = []
    for p in (posiciones or []):
        if not isinstance(p, dict):
            continue
        item = dict(p)
        if item.get("lat", item.get("latitud")) is None or item.get("lng", item.get("longitud")) is None:
            continue
        item.setdefault("placa", item.get("unidadPlaca") or "—")
        item.setdefault("unidadPlaca", item.get("placa") or "—")
        item.setdefault("letra", item.get("lineaLetra") or "—")
        item.setdefault("lineaLetra", item.get("letra") or "—")
        item.setdefault("sentido", item.get("rutaSentido") or "—")
        item.setdefault("rutaSentido", item.get("sentido") or "—")
        item.setdefault("lat", item.get("latitud"))
        item.setdefault("latitud", item.get("lat"))
        item.setdefault("lng", item.get("longitud"))
        item.setdefault("longitud", item.get("lng"))
        item.setdefault("etaMin", item.get("etaMinutos") or 0)
        item.setdefault("etaMinutos", item.get("etaMin") or 0)
        item.setdefault("proximoParadero", "—")
        item.setdefault("conductorNombre", "—")
        ocup = str(item.get("ocupacion") or "LIBRE").upper()
        item["ocupacion"] = ocup
        item["ocupacionLabel"] = (
            "Asientos libres" if ocup == "LIBRE" else ("Pasajeros de pie" if ocup == "DE_PIE" else "Micro lleno")
        )
        item["velocidadKmh"] = round(float(item.get("velocidadKmh") or 0), 1)
        normalizadas.append(item)
    return normalizadas


def obtener_posiciones_en_vivo():
    ok, data = _api_request("GET", "unidades/posicion", use_django_key=False, timeout=2.5)
    if ok and isinstance(data, list):
        return _normalizar_posiciones(data)
    return []


def actualizar_ocupacion_en_api(unidad_id, ocupacion):
    ok, data = _api_request(
        "POST",
        f"unidades/{int(unidad_id)}/ocupacion",
        payload={"ocupacion": str(ocupacion or "LIBRE").upper()},
        use_django_key=False,
        timeout=2.5,
    )
    return ok, data


def obtener_paraderos_seguros():
    ok, data = _api_request("GET", "unidades/paraderos-seguros", use_django_key=False, timeout=2.5)
    if ok and isinstance(data, list):
        return data
    return [
        {
            "paraderoId": 1,
            "nombre": "Óvalo Mansiche",
            "lat": -8.1018,
            "lng": -79.0378,
            "iluminado": True,
            "serenazgo": True,
            "comerciosCerca": True,
            "calificacion": 4.8,
            "totalReportes": 34,
            "nivelSeguridad": "ALTO",
            "recomendacion": "Paradero iluminado LED, presencia de Serenazgo y centro comercial cerca",
        },
        {
            "paraderoId": 2,
            "nombre": "Av. España - Centro Histórico",
            "lat": -8.1095,
            "lng": -79.0265,
            "iluminado": True,
            "serenazgo": True,
            "comerciosCerca": True,
            "calificacion": 4.6,
            "totalReportes": 28,
            "nivelSeguridad": "ALTO",
            "recomendacion": "Zona comercial iluminada con cámaras de monitoreo municipal",
        },
        {
            "paraderoId": 3,
            "nombre": "U.N.T. Puerta Principal",
            "lat": -8.1146,
            "lng": -79.0385,
            "iluminado": True,
            "serenazgo": True,
            "comerciosCerca": True,
            "calificacion": 4.7,
            "totalReportes": 41,
            "nivelSeguridad": "ALTO",
            "recomendacion": "Vigilancia universitaria, buena iluminación nocturna y alto flujo peatonal",
        },
        {
            "paraderoId": 4,
            "nombre": "Óvalo Papal",
            "lat": -8.1225,
            "lng": -79.0398,
            "iluminado": True,
            "serenazgo": False,
            "comerciosCerca": True,
            "calificacion": 4.3,
            "totalReportes": 19,
            "nivelSeguridad": "MEDIO",
            "recomendacion": "Buena iluminación y farmacias/bodegas abiertas hasta las 11 PM",
        },
        {
            "paraderoId": 5,
            "nombre": "Óvalo Larco",
            "lat": -8.1245,
            "lng": -79.0315,
            "iluminado": True,
            "serenazgo": True,
            "comerciosCerca": True,
            "calificacion": 4.9,
            "totalReportes": 52,
            "nivelSeguridad": "ALTO",
            "recomendacion": "Paradero seguro, patrullaje constante y comercios abiertos",
        },
    ]


def calificar_paradero_seguro(payload):
    ok, data = _api_request("POST", "unidades/paraderos-seguros", payload=payload, use_django_key=False, timeout=2.5)
    return ok, data


def obtener_pasajeros_esperando(ruta_id=None):
    """Solo pasajeros reales que transmiten su espera desde la App móvil."""
    path = f"unidades/pasajeros-esperando?rutaId={ruta_id}" if ruta_id else "unidades/pasajeros-esperando"
    ok, data = _api_request("GET", path, use_django_key=False, timeout=2.5)
    if not ok or not isinstance(data, list):
        return []
    normalizados = []
    for p in data:
        if not isinstance(p, dict):
            continue
        lat = p.get("lat", p.get("latitud"))
        lng = p.get("lng", p.get("longitud"))
        if lat is None or lng is None:
            continue
        normalizados.append({
            "pasajeroKey": p.get("pasajeroKey") or "",
            "rutaId": p.get("rutaId"),
            "nombre": p.get("nombre") or "Pasajero",
            "paraderoCercano": p.get("paraderoNombre") or p.get("paraderoCercano") or "—",
            "latitud": float(lat),
            "longitud": float(lng),
            "cantidad": int(p.get("cantidadPersonas") or p.get("cantidad") or 1),
            "enTiempoReal": bool(p.get("enVivoApp", True)),
            "tiempoEspera": p.get("tiempoEspera") or "",
        })
    return normalizados


def sincronizar_desde_api(user=None):
    """
    Sincroniza automáticamente rutas, paraderos, unidades, conductores, turnos
    e incidencias desde transporte-api (Neon PostgreSQL) hacia Django.
    """
    ok, data = _api_request("GET", "integraciones/django/sincronizacion", timeout=4.0)
    if not ok or not isinstance(data, dict):
        return False, []

    User = get_user_model()
    reporter = user if (user and user.is_authenticated) else User.objects.order_by("id").first()

    # 1. Sincronizar Rutas y Paraderos
    rutas_api = data.get("rutas") or []
    paraderos_api = data.get("paraderos") or []
    mapa_rutas = {}
    for r in rutas_api:
        r_id = r.get("id")
        codigo = r.get("codigo") or f"R-{r_id}"
        nombre = r.get("nombre") or f"Ruta {codigo}"
        color = r.get("color") or "#16845b"
        if not str(color).startswith("#") or len(str(color)) != 7:
            color = "#16845b"
        dist = r.get("distancia_km")
        route_obj, _ = Route.objects.update_or_create(
            code=codigo[:12],
            defaults={
                "name": nombre[:100],
                "color": color,
                "distance_km": Decimal(str(dist)) if dist is not None else None,
                "duration_minutes": 55,
                "active": bool(r.get("activa", True)),
            },
        )
        mapa_rutas[r_id] = route_obj

    for p in paraderos_api:
        r_id = p.get("ruta_id")
        route_obj = mapa_rutas.get(r_id)
        if not route_obj:
            continue
        orden = int(p.get("orden") or 1)
        nombre = p.get("nombre") or f"Paradero {orden}"
        ref = p.get("referencia") or ""
        lat = p.get("latitud")
        lng = p.get("longitud")
        Stop.objects.update_or_create(
            route=route_obj,
            sequence=orden,
            defaults={
                "name": nombre[:120],
                "address": ref[:200],
                "latitude": Decimal(str(lat)) if lat is not None else None,
                "longitude": Decimal(str(lng)) if lng is not None else None,
            },
        )

    for route_obj in mapa_rutas.values():
        stops = list(route_obj.stops.order_by("sequence"))
        if stops:
            route_obj.origin = stops[0].name
            route_obj.destination = stops[-1].name
            route_obj.save(update_fields=["origin", "destination"])

    # 2. Sincronizar Unidades (Flota)
    unidades_api = data.get("unidades") or []
    mapa_unidades = {}
    for u in unidades_api:
        placa = (u.get("placa") or "").strip()
        if not placa:
            continue
        padron = (u.get("numero_padron") or f"U-{u.get('id')}").strip()
        estado_raw = (u.get("estado") or "Activo").lower()
        if "mantenimiento" in estado_raw:
            status = Vehicle.Status.MAINTENANCE
        elif "baja" in estado_raw or "suspendido" in estado_raw:
            status = Vehicle.Status.INACTIVE
        else:
            status = Vehicle.Status.ACTIVE

        soat_str = str(u.get("fecha_soat_vencimiento") or "2027-06-01")[:10]
        rev_str = str(u.get("fecha_revision_tecnica_vencimiento") or "2027-06-01")[:10]

        veh_obj = Vehicle.objects.filter(plate=placa).first()
        if not veh_obj:
            # Ensure unique unit_code
            code_candidate = padron
            if Vehicle.objects.filter(unit_code=code_candidate).exists():
                code_candidate = f"{padron}-{placa}"[:20]
            veh_obj = Vehicle.objects.create(
                unit_code=code_candidate,
                plate=placa[:12],
                brand_model="Microbús Urbano Nuevo California",
                year=2023,
                capacity=int(u.get("capacidad_pasajeros") or 30),
                soat_expiry=date.fromisoformat(soat_str),
                technical_review_expiry=date.fromisoformat(rev_str),
                status=status,
            )
        else:
            veh_obj.capacity = int(u.get("capacidad_pasajeros") or veh_obj.capacity)
            veh_obj.soat_expiry = date.fromisoformat(soat_str)
            veh_obj.technical_review_expiry = date.fromisoformat(rev_str)
            veh_obj.status = status
            veh_obj.save()
        mapa_unidades[placa] = veh_obj

    # 3. Sincronizar Conductores
    conductores_api = data.get("conductores") or []
    mapa_conductores = {}
    for c in conductores_api:
        dni = (c.get("dni") or "").strip()
        if not dni:
            continue
        full_name = f"{c.get('nombres') or ''} {c.get('apellidos') or ''}".strip()
        estado_raw = (c.get("estado") or "Activo").lower()
        if "suspendido" in estado_raw:
            status = Driver.Status.SUSPENDED
        elif "baja" in estado_raw:
            status = Driver.Status.INACTIVE
        else:
            status = Driver.Status.ACTIVE

        lic_num = (c.get("numero_licencia") or f"Q{dni}").strip()
        lic_venc_str = str(c.get("licencia_vencimiento") or "2027-12-31")[:10]
        tel = (c.get("telefono") or "").strip()

        drv_obj = Driver.objects.filter(document_number=dni).first()
        if not drv_obj:
            if Driver.objects.filter(license_number=lic_num).exists():
                lic_num = f"{lic_num}-{dni}"[:30]
            drv_obj = Driver.objects.create(
                document_number=dni[:12],
                full_name=full_name[:120],
                phone=tel[:20],
                license_number=lic_num[:30],
                license_expiry=date.fromisoformat(lic_venc_str),
                status=status,
            )
        else:
            drv_obj.full_name = full_name[:120]
            drv_obj.phone = tel[:20]
            drv_obj.license_expiry = date.fromisoformat(lic_venc_str)
            drv_obj.status = status
            drv_obj.save()
        mapa_conductores[dni] = drv_obj

    # 4. Sincronizar Turnos y Despachos
    turnos_api = data.get("turnos") or []
    for t in turnos_api:
        r_id = t.get("ruta_id")
        placa = t.get("unidad_placa")
        dni = t.get("conductor_dni")
        route_obj = mapa_rutas.get(r_id)
        veh_obj = mapa_unidades.get(placa)
        drv_obj = mapa_conductores.get(dni)
        if not (route_obj and veh_obj and drv_obj):
            continue

        fecha_str = str(t.get("fecha") or date.today().isoformat())[:10]
        sal_prog_str = str(t.get("hora_salida_programada") or "07:00:00")[:8]
        estado_raw = (t.get("estado") or "Programado").lower()
        if "curso" in estado_raw or "incidencia" in estado_raw:
            shift_status = Shift.Status.IN_PROGRESS
            disp_status = Dispatch.Status.IN_PROGRESS
        elif "completado" in estado_raw:
            shift_status = Shift.Status.COMPLETED
            disp_status = Dispatch.Status.COMPLETED
        else:
            shift_status = Shift.Status.SCHEDULED
            disp_status = Dispatch.Status.SCHEDULED

        s_date = date.fromisoformat(fecha_str)
        p_time = time.fromisoformat(sal_prog_str)

        shift_obj = Shift.objects.filter(
            service_date=s_date, driver=drv_obj, planned_departure=p_time
        ).first()
        if not shift_obj:
            if not Shift.objects.filter(
                service_date=s_date, vehicle=veh_obj, planned_departure=p_time
            ).exists():
                shift_obj = Shift.objects.create(
                    service_date=s_date,
                    route=route_obj,
                    driver=drv_obj,
                    vehicle=veh_obj,
                    planned_departure=p_time,
                    status=shift_status,
                )
        else:
            if shift_obj.status != shift_status:
                shift_obj.status = shift_status
                shift_obj.save(update_fields=["status"])

        if shift_obj:
            disp_obj = Dispatch.objects.filter(shift=shift_obj, sequence=1).first()
            ahora_tz = timezone.now()
            if not disp_obj:
                Dispatch.objects.create(
                    shift=shift_obj,
                    sequence=1,
                    status=disp_status,
                    departure_at=ahora_tz if disp_status in (Dispatch.Status.IN_PROGRESS, Dispatch.Status.COMPLETED) else None,
                    arrival_at=ahora_tz if disp_status == Dispatch.Status.COMPLETED else None,
                )
            elif disp_obj.status != disp_status:
                disp_obj.status = disp_status
                if disp_status in (Dispatch.Status.IN_PROGRESS, Dispatch.Status.COMPLETED) and not disp_obj.departure_at:
                    disp_obj.departure_at = ahora_tz
                if disp_status == Dispatch.Status.COMPLETED and not disp_obj.arrival_at:
                    disp_obj.arrival_at = ahora_tz
                disp_obj.save()

    # 5. Sincronizar Incidencias reportadas desde la App / API hacia Django
    incidencias_api = data.get("incidencias") or []
    if reporter:
        for inc in incidencias_api:
            inc_django_id = int(inc.get("incidencia_django_id") or 0)
            activa = bool(inc.get("activa", True))
            cat = inc.get("categoria") or "other"
            if cat not in dict(Incident.Category.choices):
                cat = Incident.Category.OTHER
            sev = inc.get("severidad") or "medium"
            if sev not in dict(Incident.Severity.choices):
                sev = Incident.Severity.MEDIUM
            desc = inc.get("descripcion") or inc.get("titulo") or "Incidencia reportada"
            ubic = inc.get("ubicacion") or ""

            # Si es una incidencia creada originalmente en Django (ID < 100000)
            if 0 < inc_django_id < 100000:
                existing = Incident.objects.filter(pk=inc_django_id).first()
                if existing:
                    if not activa and existing.status != Incident.Status.RESOLVED:
                        existing.status = Incident.Status.RESOLVED
                        existing.resolved_at = timezone.now()
                        if not existing.resolution_notes:
                            existing.resolution_notes = "Cerrada desde la API / App móvil."
                        existing.save()
                    continue

            # Si fue reportada desde el Modo Conductor de la App (ID >= 100000)
            marker = f"[APP-{inc_django_id}]"
            existing_app = Incident.objects.filter(location__contains=marker).first()
            loc_with_marker = f"{ubic} {marker}".strip()[:200]
            if not existing_app:
                Incident.objects.create(
                    category=cat,
                    severity=sev,
                    description=desc,
                    location=loc_with_marker,
                    status=Incident.Status.OPEN if activa else Incident.Status.RESOLVED,
                    resolution_notes="" if activa else "Cerrada por el conductor en la app.",
                    resolved_at=None if activa else timezone.now(),
                    reported_by=reporter,
                )
            else:
                existing_app.category = cat
                existing_app.severity = sev
                existing_app.description = desc
                existing_app.location = loc_with_marker
                if not activa and existing_app.status != Incident.Status.RESOLVED:
                    existing_app.status = Incident.Status.RESOLVED
                    existing_app.resolved_at = timezone.now()
                    if not existing_app.resolution_notes:
                        existing_app.resolution_notes = "Cerrada por el conductor desde la app."
                elif activa and existing_app.status == Incident.Status.RESOLVED:
                    existing_app.status = Incident.Status.OPEN
                    existing_app.resolved_at = None
                existing_app.save()

    posiciones = _normalizar_posiciones(data.get("posicionesEnVivo") or [])
    return True, posiciones
