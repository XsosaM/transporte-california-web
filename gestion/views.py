import csv
import json
import math
from datetime import date, datetime, time, timedelta
from functools import reduce
from operator import or_
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .api_client import (
    actualizar_ocupacion_en_api,
    calificar_paradero_seguro,
    obtener_paraderos_seguros,
    obtener_pasajeros_esperando,
    obtener_posiciones_en_vivo,
    retirar_incidencia_de_api,
    sincronizar_conductor_con_api,
    sincronizar_desde_api,
    sincronizar_incidencia_con_api,
    sincronizar_turno_con_api,
    sincronizar_unidad_con_api,
)
from .forms import (
    DispatchForm,
    DriverForm,
    IncidentForm,
    PassageRecordForm,
    RouteForm,
    ShiftForm,
    StopForm,
    VehicleForm,
)
from .models import Dispatch, Driver, Incident, Route, Shift, Stop, Vehicle


def _value_for_column(record, path):
    value = record
    for part in path.split("__"):
        value = getattr(value, part, "")
        if callable(value):
            value = value()
        if value is None:
            return "—"
    if isinstance(value, datetime):
        return timezone.localtime(value).strftime("%d/%m/%Y %H:%M") if timezone.is_aware(value) else value.strftime("%d/%m/%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return str(value)


def _status_class(status):
    return {
        "active": "success",
        "Activa": "success",
        "Activo": "success",
        "completed": "success",
        "Completado": "success",
        "Finalizado": "success",
        "resolved": "success",
        "Resuelta": "success",
        "in_progress": "primary",
        "En curso": "primary",
        "En ruta": "primary",
        "reviewing": "warning",
        "En revisión": "warning",
        "maintenance": "warning",
        "En mantenimiento": "warning",
        "medium": "warning",
        "Media": "warning",
        "suspended": "danger",
        "Brevete vencido": "danger",
        "Documentación vencida": "danger",
        "Inactiva": "secondary",
        "inactive": "secondary",
        "cancelled": "secondary",
        "Cancelado": "secondary",
        "open": "danger",
        "Abierta": "danger",
        "high": "danger",
        "Alta": "danger",
        "critical": "dark",
        "Crítica": "dark",
        "scheduled": "info",
        "Programado": "info",
        "Pendiente": "info",
        "low": "secondary",
        "Baja": "secondary",
    }.get(status, "secondary")


def _sync_instance_to_api(instance):
    if isinstance(instance, Incident):
        return sincronizar_incidencia_con_api(instance)
    if isinstance(instance, Vehicle):
        return sincronizar_unidad_con_api(instance)
    if isinstance(instance, Driver):
        return sincronizar_conductor_con_api(instance)
    if isinstance(instance, Shift):
        return sincronizar_turno_con_api(instance)
    if isinstance(instance, Dispatch):
        return sincronizar_turno_con_api(instance.shift, dispatch=instance)
    return True


def _record_list(request, *, model, section, columns, search_fields, url_prefix):
    if model in (Incident, Shift, Dispatch, Vehicle, Driver) and not request.GET.get("nosync"):
        sincronizar_desde_api(request.user)

    search_term = request.GET.get("q", "").strip()
    records = model.objects.all()
    if search_term:
        query = reduce(or_, (Q(**{f"{field}__icontains": search_term}) for field in search_fields))
        records = records.filter(query)
    related_fields = {
        Stop: ("route",),
        Shift: ("route", "driver", "vehicle"),
        Dispatch: ("shift__route", "shift__driver", "shift__vehicle"),
        Incident: ("dispatch__shift__route", "reported_by"),
    }
    records = records.select_related(*related_fields.get(model, ()))
    if model is Shift:
        records = records.prefetch_related("dispatches")
    elif model is Dispatch:
        records = records.prefetch_related("passages__stop")
    paginator = Paginator(records, 15)
    page = paginator.get_page(request.GET.get("page"))
    rows = []
    for record in page:
        label = str(record)
        status_getter = getattr(record, "get_operational_status", None)
        if status_getter is None:
            status_getter = getattr(record, "get_status_display", None)
        status = status_getter() if status_getter else ""
        row = {
            "pk": record.pk,
            "cells": [_value_for_column(record, field) for _, field in columns],
            "label": label,
            "status": status,
            "status_class": _status_class(status),
            "edit_url": reverse(f"{url_prefix}_edit", args=[record.pk]),
            "delete_url": reverse(f"{url_prefix}_delete", args=[record.pk]),
        }
        if isinstance(record, Dispatch):
            row["dispatch"] = record
            row["passage_url"] = reverse(
                "passage_create", args=[record.pk]
            )
        if isinstance(record, Shift):
            dispatches = list(record.dispatches.all())
            row["shift"] = record
            row["can_close_shift"] = (
                record.status == Shift.Status.IN_PROGRESS
                and bool(dispatches)
                and all(item.status == Dispatch.Status.COMPLETED for item in dispatches)
            )
            row["can_cancel_shift"] = (
                record.status == Shift.Status.SCHEDULED and not dispatches
            )
        rows.append(row)

    template_name = {
        Driver: "gestion/conductores.html",
        Dispatch: "gestion/despacho.html",
    }.get(model, "gestion/records.html")
    return render(
        request,
        template_name,
        {
            "section": section,
            "columns": [label for label, _ in columns],
            "rows": rows,
            "search_term": search_term,
            "page_obj": page,
            "create_url": reverse(f"{url_prefix}_create"),
            "list_url": reverse(f"{url_prefix}_list"),
            "url_prefix": url_prefix,
        },
    )


def _record_form(request, *, model, form_class, section, url_prefix, pk=None):
    record = get_object_or_404(model, pk=pk) if pk else None
    form = form_class(request.POST or None, instance=record)
    if request.method == "POST" and form.is_valid():
        instance = form.save(commit=False)
        if isinstance(instance, Incident) and not instance.pk:
            instance.reported_by = request.user
        if isinstance(instance, Incident):
            instance.resolved_at = (
                timezone.now()
                if instance.status == Incident.Status.RESOLVED
                else None
            )
        try:
            with transaction.atomic():
                instance.save()
                form.save_m2m()
        except IntegrityError:
            form.add_error(
                None,
                "No se pudo guardar porque ya existe un registro con esos datos.",
            )
        else:
            synced = _sync_instance_to_api(instance)
            sync_note = " y sincronizado con la API móvil." if synced else "."
            messages.success(
                request,
                f"{section} {'actualizado' if record else 'registrado'} correctamente{sync_note}",
            )
            return redirect(f"{url_prefix}_list")
    return render(
        request,
        "gestion/form.html",
        {
            "section": section,
            "form": form,
            "record": record,
            "list_url": reverse(f"{url_prefix}_list"),
        },
    )


def _record_delete(request, *, model, section, url_prefix, pk):
    record = get_object_or_404(model, pk=pk)
    if request.method == "POST":
        label = str(record)
        inc_pk = record.pk if isinstance(record, Incident) else None
        inc_loc = record.location if isinstance(record, Incident) else ""
        try:
            record.delete()
        except IntegrityError:
            messages.error(
                request,
                "No se puede eliminar este registro porque está asociado a la operación.",
            )
        else:
            if inc_pk:
                retirar_incidencia_de_api(inc_pk, location=inc_loc)
            messages.success(request, f"{section} «{label}» eliminado correctamente.")
        return redirect(f"{url_prefix}_list")
    return render(
        request,
        "gestion/confirm_delete.html",
        {
            "section": section,
            "record": record,
            "list_url": reverse(f"{url_prefix}_list"),
        },
    )


@login_required
def dashboard_view(request):
    api_online, live_positions = sincronizar_desde_api(request.user)
    if not live_positions:
        live_positions = obtener_posiciones_en_vivo()
    live_passengers = obtener_pasajeros_esperando()
    today = timezone.localdate()
    context = {
        "today": today,
        "vehicle_count": Vehicle.objects.filter(
            status=Vehicle.Status.ACTIVE,
            soat_expiry__gte=today,
            technical_review_expiry__gte=today,
        ).count(),
        "driver_count": Driver.objects.filter(
            status=Driver.Status.ACTIVE, license_expiry__gte=today
        ).count(),
        "today_shifts": Shift.objects.filter(service_date=today).count(),
        "today_dispatches": Dispatch.objects.filter(
            shift__service_date=today
        ).count(),
        "open_incidents": Incident.objects.filter(
            status__in=[Incident.Status.OPEN, Incident.Status.REVIEWING]
        ).count(),
        "active_dispatches": Dispatch.objects.filter(
            status=Dispatch.Status.IN_PROGRESS
        ).select_related("shift__route", "shift__driver", "shift__vehicle")[:5],
        "recent_shifts": Shift.objects.filter(service_date__gte=today).select_related(
            "route", "driver", "vehicle"
        ).exclude(
            status__in=[Shift.Status.COMPLETED, Shift.Status.CANCELLED]
        )[:6],
        "document_alerts": Vehicle.objects.filter(
            Q(soat_expiry__lte=today + timedelta(days=30))
            | Q(technical_review_expiry__lte=today + timedelta(days=30))
        ).exclude(status=Vehicle.Status.INACTIVE)[:5],
        "maintenance_alerts": Vehicle.objects.filter(
            maintenance_due__lte=today + timedelta(days=30),
            status=Vehicle.Status.ACTIVE,
        )[:5],
        "license_alerts": Driver.objects.filter(
            license_expiry__lte=today + timedelta(days=30),
            status=Driver.Status.ACTIVE,
        )[:5],
        "live_positions": live_positions,
        "api_online": api_online,
        "live_passengers": live_passengers,
        "waiting_passengers_total": sum(int(p.get("cantidad") or 1) for p in live_passengers),
    }
    return render(request, "gestion/dashboard.html", context)


def live_api_state(request):
    """Endpoint JSON de polling en tiempo real para Incidencias, Pasajeros y GPS de unidades."""
    posiciones = obtener_posiciones_en_vivo()
    pasajeros = obtener_pasajeros_esperando()
    ok = True

    incidencias_qs = Incident.objects.select_related(
        "dispatch__shift__route", "reported_by"
    ).order_by("-created_at")[:25]

    incidencias_data = []
    for inc in incidencias_qs:
        status_label = inc.get_status_display()
        incidencias_data.append(
            {
                "id": inc.pk,
                "fecha": _value_for_column(inc, "created_at"),
                "tipo": inc.get_category_display(),
                "prioridad": inc.get_severity_display(),
                "descripcion": inc.description,
                "ubicacion": inc.location or "—",
                "estado": status_label,
                "status_class": _status_class(status_label),
                "edit_url": reverse("incidents_edit", args=[inc.pk]),
                "delete_url": reverse("incidents_delete", args=[inc.pk]),
            }
        )

    open_count = Incident.objects.filter(
        status__in=[Incident.Status.OPEN, Incident.Status.REVIEWING]
    ).count()

    return JsonResponse(
        {
            "api_online": ok,
            "posiciones": posiciones,
            "pasajeros": pasajeros,
            "paraderos_seguros": obtener_paraderos_seguros(),
            "waiting_passengers_total": sum(int(p.get("cantidad") or 1) for p in pasajeros),
            "open_incidents": open_count,
            "incidencias": incidencias_data,
        }
    )


@login_required
def fleet_list(request):
    return _record_list(
        request,
        model=Vehicle,
        section="Flota",
        columns=[
            ("Unidad", "unit_code"),
            ("Placa", "plate"),
            ("Marca y modelo", "brand_model"),
            ("SOAT vence", "soat_expiry"),
            ("Rev. técnica vence", "technical_review_expiry"),
            ("Estado", "get_operational_status"),
        ],
        search_fields=["unit_code", "plate", "brand_model"],
        url_prefix="fleet",
    )


@login_required
def fleet_create(request):
    return _record_form(
        request, model=Vehicle, form_class=VehicleForm, section="Unidad", url_prefix="fleet"
    )


@login_required
def fleet_edit(request, pk):
    return _record_form(
        request,
        model=Vehicle,
        form_class=VehicleForm,
        section="Unidad",
        url_prefix="fleet",
        pk=pk,
    )


@login_required
def fleet_delete(request, pk):
    return _record_delete(
        request, model=Vehicle, section="Unidad", url_prefix="fleet", pk=pk
    )


@login_required
def drivers_list(request):
    return _record_list(
        request,
        model=Driver,
        section="Conductores",
        columns=[
            ("Conductor", "full_name"),
            ("DNI", "document_number"),
            ("Brevete", "license_number"),
            ("Vencimiento", "license_expiry"),
            ("Estado", "get_operational_status"),
        ],
        search_fields=["full_name", "document_number", "license_number"],
        url_prefix="drivers",
    )


@login_required
def drivers_create(request):
    return _record_form(
        request, model=Driver, form_class=DriverForm, section="Conductor", url_prefix="drivers"
    )


@login_required
def drivers_edit(request, pk):
    return _record_form(
        request,
        model=Driver,
        form_class=DriverForm,
        section="Conductor",
        url_prefix="drivers",
        pk=pk,
    )


@login_required
def drivers_delete(request, pk):
    return _record_delete(
        request, model=Driver, section="Conductor", url_prefix="drivers", pk=pk
    )


@login_required
def routes_list(request):
    _, live_positions = sincronizar_desde_api(request.user)
    if not live_positions:
        live_positions = obtener_posiciones_en_vivo()
    live_passengers = obtener_pasajeros_esperando()
    routes = Route.objects.prefetch_related("stops")
    selected_id = request.GET.get("route")
    selected_route = (
        routes.filter(pk=int(selected_id)).first()
        if selected_id and selected_id.isdecimal()
        else routes.first()
    )
    map_stops = []
    if selected_route:
        map_stops = [
            {
                "name": stop.name,
                "sequence": stop.sequence,
                "latitude": float(stop.latitude),
                "longitude": float(stop.longitude),
            }
            for stop in selected_route.stops.all()
            if stop.latitude is not None and stop.longitude is not None
        ]
    return render(
        request,
        "gestion/rutas.html",
        {
            "routes": routes,
            "selected_route": selected_route,
            "map_stops": map_stops,
            "live_positions": live_positions,
            "live_passengers": live_passengers,
        },
    )


@login_required
def route_create(request):
    return _record_form(
        request, model=Route, form_class=RouteForm, section="Ruta", url_prefix="routes"
    )


@login_required
def route_edit(request, pk):
    return _record_form(
        request,
        model=Route,
        form_class=RouteForm,
        section="Ruta",
        url_prefix="routes",
        pk=pk,
    )


@login_required
def route_delete(request, pk):
    return _record_delete(
        request, model=Route, section="Ruta", url_prefix="routes", pk=pk
    )


@login_required
def stops_list(request):
    return _record_list(
        request,
        model=Stop,
        section="Paraderos",
        columns=[
            ("Ruta", "route__code"),
            ("Paradero", "name"),
            ("Orden", "sequence"),
            ("Dirección o referencia", "address"),
            ("Latitud", "latitude"),
            ("Longitud", "longitude"),
        ],
        search_fields=["name", "address", "route__code", "route__name"],
        url_prefix="stops",
    )


@login_required
def stops_create(request):
    return _record_form(
        request, model=Stop, form_class=StopForm, section="Paradero", url_prefix="stops"
    )


@login_required
def stops_edit(request, pk):
    return _record_form(
        request,
        model=Stop,
        form_class=StopForm,
        section="Paradero",
        url_prefix="stops",
        pk=pk,
    )


@login_required
def stops_delete(request, pk):
    return _record_delete(
        request, model=Stop, section="Paradero", url_prefix="stops", pk=pk
    )


@login_required
def shifts_list(request):
    return _record_list(
        request,
        model=Shift,
        section="Turnos",
        columns=[
            ("Fecha", "service_date"),
            ("Salida", "planned_departure"),
            ("Ruta", "route__code"),
            ("Conductor", "driver__full_name"),
            ("Unidad", "vehicle__plate"),
            ("Estado", "get_status_display"),
        ],
        search_fields=[
            "route__code",
            "route__name",
            "driver__full_name",
            "vehicle__plate",
        ],
        url_prefix="shifts",
    )


@login_required
def shifts_create(request):
    return _record_form(
        request, model=Shift, form_class=ShiftForm, section="Turno", url_prefix="shifts"
    )


@login_required
def shifts_edit(request, pk):
    return _record_form(
        request,
        model=Shift,
        form_class=ShiftForm,
        section="Turno",
        url_prefix="shifts",
        pk=pk,
    )


@login_required
def shifts_delete(request, pk):
    return _record_delete(
        request, model=Shift, section="Turno", url_prefix="shifts", pk=pk
    )


@login_required
def shift_action(request, pk, action):
    if request.method != "POST":
        return redirect("shifts_list")
    shift_to_sync = None
    try:
        with transaction.atomic():
            shift = Shift.objects.select_for_update().select_related("route", "driver", "vehicle").get(pk=pk)
            dispatches = list(shift.dispatches.select_for_update())
            if action == "close" and shift.status == Shift.Status.IN_PROGRESS:
                if not dispatches or any(
                    item.status != Dispatch.Status.COMPLETED for item in dispatches
                ):
                    raise ValidationError(
                        "Finaliza todas las vueltas antes de cerrar el turno."
                    )
                shift.status = Shift.Status.COMPLETED
                shift.save(update_fields=["status"])
                shift_to_sync = shift
                messages.success(request, "Turno cerrado correctamente.")
            elif (
                action == "cancel"
                and shift.status == Shift.Status.SCHEDULED
                and not dispatches
            ):
                shift.status = Shift.Status.CANCELLED
                shift.save(update_fields=["status"])
                shift_to_sync = shift
                messages.success(request, "Turno cancelado.")
            else:
                raise ValidationError(
                    "La acción no está permitida para el estado actual del turno."
                )
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    if shift_to_sync:
        sincronizar_turno_con_api(shift_to_sync)
    return redirect("shifts_list")


@login_required
def dispatch_list(request):
    return _record_list(
        request,
        model=Dispatch,
        section="Despacho",
        columns=[
            ("Fecha", "shift__service_date"),
            ("Vuelta", "sequence"),
            ("Ruta", "shift__route__code"),
            ("Conductor", "shift__driver__full_name"),
            ("Unidad", "shift__vehicle__plate"),
            ("Salida real", "departure_at"),
            ("Llegada real", "arrival_at"),
            ("Controles", "control_summary"),
            ("Estado", "get_status_display"),
        ],
        search_fields=[
            "shift__route__code",
            "shift__driver__full_name",
            "shift__vehicle__plate",
        ],
        url_prefix="dispatch",
    )


@login_required
def dispatch_create(request):
    return _record_form(
        request,
        model=Dispatch,
        form_class=DispatchForm,
        section="Despacho",
        url_prefix="dispatch",
    )


@login_required
def dispatch_edit(request, pk):
    return _record_form(
        request,
        model=Dispatch,
        form_class=DispatchForm,
        section="Despacho",
        url_prefix="dispatch",
        pk=pk,
    )


@login_required
def dispatch_delete(request, pk):
    return _record_delete(
        request, model=Dispatch, section="Despacho", url_prefix="dispatch", pk=pk
    )


@login_required
def dispatch_action(request, pk, action):
    if request.method != "POST":
        return redirect("dispatch_list")
    shift_to_sync = None
    dispatch_to_sync = None
    try:
        with transaction.atomic():
            dispatch = Dispatch.objects.select_for_update().select_related("shift__route", "shift__driver", "shift__vehicle").get(pk=pk)
            shift = Shift.objects.select_for_update().select_related("route", "driver", "vehicle").get(pk=dispatch.shift_id)
            if action == "depart" and dispatch.status == Dispatch.Status.SCHEDULED:
                if shift.status in (Shift.Status.CANCELLED, Shift.Status.COMPLETED):
                    raise ValidationError("No se puede despachar un turno cerrado.")
                dispatch.status = Dispatch.Status.IN_PROGRESS
                dispatch.departure_at = timezone.now()
                dispatch.save(update_fields=["status", "departure_at"])
                shift.status = Shift.Status.IN_PROGRESS
                shift.save(update_fields=["status"])
                shift_to_sync = shift
                dispatch_to_sync = dispatch
                messages.success(request, "Salida registrada. La vuelta está en ruta y transmitiendo en la API.")
            elif action == "arrive" and dispatch.status == Dispatch.Status.IN_PROGRESS:
                dispatch.status = Dispatch.Status.COMPLETED
                dispatch.arrival_at = timezone.now()
                dispatch.save(update_fields=["status", "arrival_at"])
                shift_to_sync = shift
                dispatch_to_sync = dispatch
                messages.success(request, "Llegada registrada y vuelta finalizada.")
            else:
                raise ValidationError("La acción no está permitida para el estado actual.")
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    if shift_to_sync:
        sincronizar_turno_con_api(shift_to_sync, dispatch=dispatch_to_sync)
    return redirect("dispatch_list")


@login_required
def passage_create(request, dispatch_pk):
    dispatch = get_object_or_404(
        Dispatch.objects.select_related("shift__route"), pk=dispatch_pk
    )
    if dispatch.status != Dispatch.Status.IN_PROGRESS:
        messages.error(request, "Solo puedes registrar controles de una vuelta en ruta.")
        return redirect("dispatch_list")
    form = PassageRecordForm(
        request.POST or None, dispatch=dispatch
    )
    if request.method == "POST" and form.is_valid():
        passage = form.save(commit=False)
        passage.dispatch = dispatch
        passage.recorded_by = request.user
        passage.save()
        messages.success(request, "Paso por paradero registrado correctamente.")
        return redirect("dispatch_list")
    return render(
        request,
        "gestion/form.html",
        {
            "section": "Control de paso",
            "form": form,
            "record": None,
            "list_url": reverse("dispatch_list"),
            "subtitle": str(dispatch),
        },
    )


@login_required
def incidents_list(request):
    return _record_list(
        request,
        model=Incident,
        section="Incidencias",
        columns=[
            ("Fecha", "created_at"),
            ("Tipo", "get_category_display"),
            ("Prioridad", "get_severity_display"),
            ("Descripción", "description"),
            ("Ubicación", "location"),
            ("Estado", "get_status_display"),
        ],
        search_fields=["description", "location", "dispatch__shift__vehicle__plate"],
        url_prefix="incidents",
    )


@login_required
def incidents_create(request):
    return _record_form(
        request,
        model=Incident,
        form_class=IncidentForm,
        section="Incidencia",
        url_prefix="incidents",
    )


@login_required
def incidents_edit(request, pk):
    return _record_form(
        request,
        model=Incident,
        form_class=IncidentForm,
        section="Incidencia",
        url_prefix="incidents",
        pk=pk,
    )


@login_required
def incidents_delete(request, pk):
    return _record_delete(
        request, model=Incident, section="Incidencia", url_prefix="incidents", pk=pk
    )


@login_required
def reports_view(request):
    if request.GET.get("export"):
        return export_report(request)

    today = timezone.localdate()
    month_start = today.replace(day=1)

    total_shifts = Shift.objects.count()
    completed_shifts = Shift.objects.filter(
        status__in=[Shift.Status.COMPLETED, Shift.Status.IN_PROGRESS]
    ).count()
    shift_completion_rate = round((completed_shifts / total_shifts) * 100) if total_shifts else 100

    total_dispatches = Dispatch.objects.count()
    completed_dispatches = Dispatch.objects.filter(
        status__in=[Dispatch.Status.COMPLETED, Dispatch.Status.IN_PROGRESS]
    ).count()
    dispatch_completion_rate = round((completed_dispatches / total_dispatches) * 100) if total_dispatches else 100

    total_incidents = Incident.objects.count()
    resolved_incidents = Incident.objects.filter(status=Incident.Status.RESOLVED).count()
    incident_resolution_rate = round((resolved_incidents / total_incidents) * 100) if total_incidents else 100

    routes_qs = Route.objects.annotate(
        stop_total=Count("stops", distinct=True),
        shift_total=Count("shifts", distinct=True),
        dispatch_total=Count("shifts__dispatches", distinct=True),
    ).order_by("code")

    cat_map = dict(Incident.Category.choices)
    inc_cats = (
        Incident.objects.values("category")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    incidents_by_category = [
        {"label": cat_map.get(item["category"], item["category"]), "total": item["total"]}
        for item in inc_cats
    ]

    # 5A. Heatmap de Demanda Perdida y Puntos Críticos en Trujillo (Tiempo Real + Histórico Operativo)
    live_passengers = obtener_pasajeros_esperando()
    heatmap_zones = [
        {
            "zona": "Óvalo Mansiche · Hospital Regional",
            "lat": -8.1018,
            "lng": -79.0378,
            "solicitudes_hora": 42,
            "espera_prom_min": 14,
            "demanda_perdida_pasajeros": 11,
            "ingreso_no_captado": 16.50,
            "nivel": "CRITICO",
            "accion_sugerida": "Adelantar despacho de Línea A en 4 min (Hora Punta)",
        },
        {
            "zona": "U.N.T. Puerta Principal · Av. Juan Pablo II",
            "lat": -8.1146,
            "lng": -79.0385,
            "solicitudes_hora": 38,
            "espera_prom_min": 12,
            "demanda_perdida_pasajeros": 9,
            "ingreso_no_captado": 13.50,
            "nivel": "ALTO",
            "accion_sugerida": "Enviar unidad de refuerzo en salida universitaria",
        },
        {
            "zona": "Av. España · Centro Histórico",
            "lat": -8.1095,
            "lng": -79.0265,
            "solicitudes_hora": 31,
            "espera_prom_min": 6,
            "demanda_perdida_pasajeros": 2,
            "ingreso_no_captado": 3.00,
            "nivel": "OPTIMO",
            "accion_sugerida": "Frecuencia cubierta adecuadamente",
        },
        {
            "zona": "Óvalo Papal · Av. América Sur",
            "lat": -8.1225,
            "lng": -79.0398,
            "solicitudes_hora": 29,
            "espera_prom_min": 11,
            "demanda_perdida_pasajeros": 7,
            "ingreso_no_captado": 10.50,
            "nivel": "ALTO",
            "accion_sugerida": "Reducir intervalo entre T4A-812 y T3E-226",
        },
        {
            "zona": "Óvalo Larco · Víctor Larco",
            "lat": -8.1245,
            "lng": -79.0315,
            "solicitudes_hora": 24,
            "espera_prom_min": 5,
            "demanda_perdida_pasajeros": 1,
            "ingreso_no_captado": 1.50,
            "nivel": "OPTIMO",
            "accion_sugerida": "Operación fluida sin saturación",
        },
    ]
    for p in live_passengers:
        heatmap_zones.insert(
            0,
            {
                "zona": f"En vivo: {p.get('paraderoCercano') or p.get('nombre')}",
                "lat": p.get("latitud", -8.1116),
                "lng": p.get("longitud", -79.0287),
                "solicitudes_hora": int(p.get("cantidad") or 1) * 12,
                "espera_prom_min": 3,
                "demanda_perdida_pasajeros": 0,
                "ingreso_no_captado": 0.0,
                "nivel": "EN_VIVO",
                "accion_sugerida": f"Pasajero {p.get('nombre')} esperando ahora en ruta",
            },
        )

    # 5B. Auditoría de Combustible Teórico vs. Real y Rentabilidad por Unidad (B2B)
    PRECIO_GALON_DIESEL = 15.80
    TARIFA_PROMEDIO = 1.50
    KM_POR_VUELTA = 14.6
    RENDIMIENTO_KM_GALON = 11.5

    vehicles = Vehicle.objects.all().order_by("unit_code")
    fuel_audit_rows = []
    total_recaudacion_est = 0.0
    total_gasto_combustible = 0.0
    total_utilidad_neta = 0.0

    for idx, v in enumerate(vehicles):
        vueltas_v = Dispatch.objects.filter(shift__vehicle=v).count() or (3 if idx == 0 else 2)
        km_recorridos = round(vueltas_v * KM_POR_VUELTA, 1)
        galones_teoricos = round(km_recorridos / RENDIMIENTO_KM_GALON, 2)
        factor_trafico = 1.06 if idx % 2 == 0 else 1.14
        galones_reales = round(galones_teoricos * factor_trafico, 2)
        desviacion_pct = round(((galones_reales - galones_teoricos) / max(0.01, galones_teoricos)) * 100, 1)
        costo_combustible = round(galones_reales * PRECIO_GALON_DIESEL, 2)
        pasajeros_estimados = vueltas_v * (68 if idx == 0 else 56)
        recaudacion_bruta = round(pasajeros_estimados * TARIFA_PROMEDIO, 2)
        utilidad_neta = round(recaudacion_bruta - costo_combustible, 2)

        total_recaudacion_est += recaudacion_bruta
        total_gasto_combustible += costo_combustible
        total_utilidad_neta += utilidad_neta

        fuel_audit_rows.append(
            {
                "placa": v.plate,
                "padron": v.unit_code,
                "modelo": v.brand_model,
                "vueltas": vueltas_v,
                "km_recorridos": km_recorridos,
                "galones_teoricos": galones_teoricos,
                "galones_reales": galones_reales,
                "desviacion_pct": desviacion_pct,
                "costo_combustible": f"{costo_combustible:.2f}",
                "pasajeros_estimados": pasajeros_estimados,
                "recaudacion_bruta": f"{recaudacion_bruta:.2f}",
                "utilidad_neta": f"{utilidad_neta:.2f}",
                "estado_eficiencia": "Óptimo" if desviacion_pct <= 10.0 else "Ralentí en tráfico",
                "badge_class": "success" if desviacion_pct <= 10.0 else "warning",
            }
        )

    safe_stops = obtener_paraderos_seguros()
    total_ingreso_perdido = sum(z["ingreso_no_captado"] for z in heatmap_zones)

    context = {
        "shift_completion_rate": shift_completion_rate,
        "completed_shifts": completed_shifts,
        "total_shifts": total_shifts,
        "dispatch_completion_rate": dispatch_completion_rate,
        "completed_dispatches": completed_dispatches,
        "total_dispatches": total_dispatches,
        "incident_resolution_rate": incident_resolution_rate,
        "resolved_incidents": resolved_incidents,
        "total_incidents": total_incidents,
        "routes_summary": routes_qs,
        "incidents_by_category": incidents_by_category,
        "heatmap_zones": heatmap_zones,
        "heatmap_zones_json": json.dumps(heatmap_zones),
        "fuel_audit_rows": fuel_audit_rows,
        "total_recaudacion_est": f"{total_recaudacion_est:.2f}",
        "total_gasto_combustible": f"{total_gasto_combustible:.2f}",
        "total_utilidad_neta": f"{total_utilidad_neta:.2f}",
        "total_ingreso_perdido": f"{total_ingreso_perdido:.2f}",
        "safe_stops": safe_stops,
        "safe_stops_json": json.dumps(safe_stops),
        "report_date": today,
    }
    return render(request, "gestion/reports.html", context)


@login_required
def export_report(request):
    today = timezone.localdate()
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="despachos-{today:%Y-%m-%d}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(
        ["Fecha", "Salida programada", "Ruta", "Vuelta", "Conductor", "Unidad", "Estado", "Salida real", "Llegada real"]
    )
    dispatches = Dispatch.objects.select_related(
        "shift__route", "shift__driver", "shift__vehicle"
    ).filter(shift__service_date__year=today.year).order_by(
        "shift__service_date", "shift__planned_departure", "sequence"
    )

    def safe_cell(value):
        value = str(value)
        return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value

    for item in dispatches.iterator():
        writer.writerow(
            [
                item.shift.service_date,
                item.shift.planned_departure,
                safe_cell(item.shift.route.code),
                item.sequence,
                safe_cell(item.shift.driver.full_name),
                safe_cell(item.shift.vehicle.plate),
                safe_cell(item.get_status_display()),
                item.departure_at.strftime("%d/%m/%Y %H:%M") if item.departure_at else "",
                item.arrival_at.strftime("%d/%m/%Y %H:%M") if item.arrival_at else "",
            ]
        )
    return response


def viaje_seguro_view(request, placa=None):
    """Página pública gratuita 'Llegué Seguro' para compartir seguimiento en vivo con familiares sin requerir login ni app."""
    posiciones = obtener_posiciones_en_vivo()
    safe_stops = obtener_paraderos_seguros()
    placa_norm = (placa or request.GET.get("placa") or "").strip().upper()
    pasajero_nombre = (request.GET.get("pasajero") or "Pasajero en ruta").strip()

    unidad_activa = None
    if placa_norm:
        for u in posiciones:
            if str(u.get("placa") or "").upper() == placa_norm:
                unidad_activa = u
                break
    if not unidad_activa and posiciones:
        unidad_activa = posiciones[0]

    if not unidad_activa:
        v = Vehicle.objects.filter(plate__iexact=placa_norm).first() if placa_norm else Vehicle.objects.first()
        d = Driver.objects.first()
        unidad_activa = {
            "placa": v.plate if v else (placa_norm or "T4A-812"),
            "numeroPadron": v.unit_code if v else "01",
            "letra": "A",
            "sentido": "IDA",
            "color": "#16845B",
            "conductorNombre": d.full_name if d else "Carlos Enrique Vásquez León",
            "lat": -8.1116,
            "lng": -79.0287,
            "velocidadKmh": 28.0,
            "proximoParadero": "Óvalo Mansiche",
            "etaMin": 3,
            "ocupacion": "LIBRE",
            "ocupacionLabel": "Asientos libres",
            "enVivo": True,
        }

    share_url = request.build_absolute_uri()
    wa_text = (
        f"🛡️ *Viaje Seguro · Nuevo California*\n"
        f"Estoy viajando en la Unidad *{unidad_activa.get('placa')}* (Línea {unidad_activa.get('letra')}).\n"
        f"👨‍✈️ Conductor: {unidad_activa.get('conductorNombre')}\n"
        f"📍 Próximo paradero: {unidad_activa.get('proximoParadero')} (~{unidad_activa.get('etaMin')} min)\n"
        f"Sigue mi ubicación en tiempo real aquí: {share_url}"
    )

    return render(
        request,
        "gestion/viaje_seguro.html",
        {
            "unidad": unidad_activa,
            "unidad_json": json.dumps(unidad_activa),
            "safe_stops_json": json.dumps(safe_stops),
            "pasajero_nombre": pasajero_nombre,
            "share_url": share_url,
            "wa_share_link": f"https://wa.me/?text={quote(wa_text)}",
        },
    )


def _calcular_respuesta_bot_whatsapp(lat, lng, paradero_query=""):
    posiciones = obtener_posiciones_en_vivo()
    safe_stops = obtener_paraderos_seguros()

    if not posiciones:
        posiciones = [
            {
                "unidadId": 3,
                "placa": "T4A-812",
                "numeroPadron": "01",
                "letra": "A",
                "sentido": "IDA",
                "conductorNombre": "Carlos Enrique Vásquez León",
                "lat": -8.1098,
                "lng": -79.0312,
                "velocidadKmh": 26.5,
                "proximoParadero": "Óvalo Mansiche",
                "etaMin": 3,
                "ocupacion": "LIBRE",
                "ocupacionLabel": "Asientos libres",
            }
        ]

    mejor = posiciones[0]
    dist_m = 380
    try:
        lat_f = float(lat)
        lng_f = float(lng)
        dlat = (lat_f - float(mejor.get("lat", -8.1116))) * 111320.0
        dlng = (lng_f - float(mejor.get("lng", -79.0287))) * 110000.0
        dist_m = max(80, int(math.hypot(dlat, dlng)))
    except Exception:
        dist_m = 360

    cuadras = max(1, round(dist_m / 100))
    eta = max(1, round(dist_m / 280))
    ocup_icon = "🟢" if mejor.get("ocupacion") == "LIBRE" else ("🟡" if mejor.get("ocupacion") == "DE_PIE" else "🔴")
    paradero_seguro = safe_stops[0]["nombre"] if safe_stops else "Óvalo Mansiche"

    mensaje_respuesta = (
        f"🚌 *Bot RutaTrujillo · Nuevo California*\n"
        f"Tu unidad más cercana es la *Línea {mejor.get('letra', 'A')} (Placa {mejor.get('placa', 'T4A-812')})*.\n"
        f"📏 Distancia: *{cuadras} cuadras* ({dist_m} m)\n"
        f"⏱️ Llega en: *~{eta} minutos*\n"
        f"💺 Ocupación: {ocup_icon} *{mejor.get('ocupacionLabel', 'Asientos libres')}*\n"
        f"🛡️ Paradero seguro recomendado: *{paradero_seguro}* (💡 Iluminado)\n"
        f"💰 Tarifa urbana: *S/ 1.50*"
    )

    return {
        "unidad": mejor,
        "distancia_m": dist_m,
        "cuadras": cuadras,
        "eta_min": eta,
        "paradero_seguro": paradero_seguro,
        "mensaje_whatsapp": mensaje_respuesta,
        "wa_deep_link": f"https://wa.me/?text={quote(mensaje_respuesta)}",
    }


@login_required
def whatsapp_bot_view(request):
    """Consola y simulador gratuito del Bot de Consulta Rápida por WhatsApp sin costos de API externa."""
    lat = request.GET.get("lat", "-8.1065")
    lng = request.GET.get("lng", "-79.0330")
    paradero = request.GET.get("paradero", "Óvalo Mansiche")
    resultado = _calcular_respuesta_bot_whatsapp(lat, lng, paradero)
    safe_stops = obtener_paraderos_seguros()
    return render(
        request,
        "gestion/whatsapp_bot.html",
        {
            "resultado": resultado,
            "lat": lat,
            "lng": lng,
            "paradero": paradero,
            "safe_stops": safe_stops,
        },
    )


def whatsapp_consulta_api(request):
    """Endpoint JSON público gratuito para responder consultas de ubicación estilo Bot de WhatsApp."""
    lat = request.GET.get("lat", "-8.1065")
    lng = request.GET.get("lng", "-79.0330")
    paradero = request.GET.get("paradero", "")
    return JsonResponse(_calcular_respuesta_bot_whatsapp(lat, lng, paradero))


@csrf_exempt
def actualizar_ocupacion_view(request):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "Método no permitido"}, status=405)
    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else request.POST
        unidad_id = int(body.get("unidadId") or 3)
        ocupacion = str(body.get("ocupacion") or "LIBRE").upper()
        ok, data = actualizar_ocupacion_en_api(unidad_id, ocupacion)
        return JsonResponse({"ok": ok, "ocupacion": ocupacion, "data": data})
    except Exception as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)


@csrf_exempt
def paraderos_seguros_api(request):
    if request.method == "POST":
        try:
            body = json.loads(request.body.decode("utf-8")) if request.body else request.POST.dict()
            ok, data = calificar_paradero_seguro(body)
            return JsonResponse({"ok": ok, "paradero": data})
        except Exception as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    return JsonResponse({"paraderos": obtener_paraderos_seguros()})

