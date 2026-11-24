# 📜 Registro Histórico de Cambios (CHANGELOG)
Todos los cambios notables, hitos de ingeniería y nuevas características del proyecto **Nuevo California & TruxiGo** se documentan en este archivo.

El formato se basa en [Keep a Changelog](https://keepachangelog.com/es-ES/1.0.0/) y adhiere a [Versionado Semántico (SemVer)](https://semver.org/lang/es/).

---

## [v2.0.0] - 2026-11-24 (Despliegue Cloud & Optimización Móvil)
### 🚀 Añadido
- **Despliegue en Producción (Cloud Render):**
  - Configuración nativa de servidor WSGI de alto rendimiento con **Gunicorn**.
  - Pipeline de entrega de archivos estáticos optimizado con compresión y caché mediante **WhiteNoise**.
  - Certificado SSL/TLS con HTTPS permanente y protección Cloudflare: [https://gestion-transporte-california.onrender.com](https://gestion-transporte-california.onrender.com).
- **Diseño 100% Responsive para Smartphones y Tablets:**
  - Despliegue de menú lateral tipo **Offcanvas Drawer táctil** con efecto de desenfoque (*backdrop blur*).
  - Encabezado superior (*topbar*) adaptable y adherente (`sticky`), con botones de acción compactos.
  - Rejilla fluida de tarjetas de métricas (KPIs) con tamaño dinámico `clamp()` para cifras monetarias y contadores.
  - Redibujado automático de mapas interactivos GPS (Leaflet / Google Maps) al rotar pantalla o abrir el menú.

### 🎨 Mejoras de Marca & UI Móvil (TruxiGo)
- Rediseño del launcher icon HD 4K de Android con soporte adaptativo completo (`mipmap-anydpi-v26`).
- Implementación de **Cápsula Blanca Premium** con esquinas redondeadas y elevación para el logotipo en pantallas de Login, Registro e Ingreso Conductor, garantizando máximo contraste y fidelidad de color tanto en modo claro como en modo oscuro.

---

## [v1.3.0] - 2026-11-23 (Microservicios & Interfaz Ejecutiva)
### ⚙️ Arquitectura & Backend
- **Microservicio de Notificaciones y Alertas (`transporte-notificaciones`):**
  - Servicio autónomo desarrollado en **Spring Boot 3.3.4 (Java 21)** ejecutándose en puerto `8081`.
  - Cola en memoria de incidentes y alertas para supervisión en tiempo real.
  - Endpoints REST para ingesta de incidencias mecánicas, seguridad vial y alertas de pasajeros.
- **Conectividad entre Servicios:**
  - Comunicación desacoplada entre la API central (`transporte-api`: 8080) y el microservicio de notificaciones (`transporte-notificaciones`: 8081).

### 🖥️ Panel Web Administrativo
- **Rediseño Corporativo Esmeralda:**
  - Paleta de color institucional verde esmeralda profundo (`#06231c`, `#059669`) con elevación sutil y estilo *glassmorphism*.
  - Indicador de estado de API en vivo con anillo de pulso luminoso (*pulse ring*).
  - Alertas sonoras sutiles implementadas con **Web Audio API** nativo (sin librerías pesadas) para eventos críticos de flota.

---

## [v1.2.0] - 2026-11-22 (Innovación Operativa & Analítica B2B)
### 💡 Nuevos Módulos
- **Viaje Seguro:** Módulo ciudadano para compartir código de viaje y ubicación de unidad en tiempo real con familiares.
- **Bot de Consulta WhatsApp:** Enlace dinámico directo para que los usuarios pregunten por el microbús más cercano vía webhook.
- **Analítica de Rentabilidad B2B:**
  - Cálculo automático de Recaudación Bruta Estimada (tarifa S/ 1.50).
  - Auditoría de gasto de combustible por kilometraje GPS vs. ralentí.
  - Estimación de Demanda No Captada por tiempos de espera superiores a 10 minutos.

---

## [v1.1.0] - 2026-11-15 (Ecosistema Móvil Android Jetpack Compose)
### 📱 Aplicación Móvil TruxiGo
- Arquitectura moderna en **Android (Kotlin + Jetpack Compose + Material 3)**.
- Dos sabores operativos compilables independientes:
  - `app-pasajero-debug.apk`: Búsqueda de rutas, paraderos cercanos, tiempo estimado de llegada y botón de pánico.
  - `app-conductor-debug.apk`: Inicio de ruta, reporte de posición GPS en tiempo real y registro de incidencias en ruta.
- Consumo de API REST Spring Boot para sincronización instantánea de flota.

---

## [v1.0.0] - 2026-10-09 (Lanzamiento Inicial del Sistema)
### 📦 Funcionalidades Base
- Panel administrativo inicial en Django (Python).
- Modelado de datos relacional para Empresas, Flota, Conductores, Rutas, Paraderos, Turnos y Despachos.
- Sistema de autenticación y control de acceso para supervisores y administradores.
- Exportación básica de despachos en formato CSV.
- Integración cartográfica inicial con Leaflet y OpenStreetMap.
