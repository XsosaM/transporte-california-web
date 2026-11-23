from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include

admin.site.site_header = "Nuevo California S.A. — Administración Operacional"
admin.site.site_title = "Nuevo California"
admin.site.index_title = "Gestión de Datos y Operaciones"

urlpatterns = [
    path('admin/', admin.site.urls),
    path('acceso/', auth_views.LoginView.as_view(template_name='gestion/login.html'), name='login'),
    path('login/', auth_views.LoginView.as_view(template_name='gestion/login.html')),
    path('salir/', auth_views.LogoutView.as_view(), name='logout'),
    path('logout/', auth_views.LogoutView.as_view()),
    path('', include('gestion.urls')),
]
