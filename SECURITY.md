# Seguridad y privacidad

Este documento resume la revisión de seguridad del código de la integración
y qué riesgos existen de que **alguien pueda ver la posición de tus SmartTag**.

## 1. ¿Hay puertas traseras o envío de datos a terceros?

No. Se ha revisado todo `custom_components/smartthings_find/`:

| Comprobación | Resultado |
|---|---|
| Hosts a los que se conecta | Sólo `account.samsung.com` (login) y `smartthingsfind.samsung.com` (datos). No hay telemetría, analítica ni servidores propios. |
| Redirecciones durante el login | Las dos URLs que llegan dentro de las respuestas de Samsung se validan: sólo se siguen si son `https` y del host exacto de Samsung. Así el código de autorización nunca puede acabar en un tercero (hay tests para ello). |
| TLS | Siempre `https` con verificación de certificados (contexto TLS de Home Assistant). |
| Ejecución dinámica de código | No hay `eval`, `exec`, `subprocess`, `pickle` ni imports dinámicos. |
| Escritura en disco | Ninguna propia. Home Assistant guarda la configuración de la entrada (ver punto 2). |
| Registros (logs) | No se registran coordenadas, la cookie ni el token CSRF. Los errores sólo incluyen la ruta del endpoint y el código HTTP, nunca el cuerpo de la respuesta. |
| Cookies | Sesiones HTTP aisladas con su propio cookie jar: la cookie de Samsung no se mezcla con otras integraciones. Las sesiones del login se cierran nada más terminar. |
| Contraseña | Nunca pasa por Home Assistant: el login es por QR y se aprueba en el móvil. |
| Iconos | Sólo se muestran iconos `https` de dominios de Samsung. Cualquier otra URL se descarta, porque el navegador de quien abra Home Assistant la descargaría (riesgo de "píxel espía"). |
| Enlace del QR | Sólo se acepta `https://signin.samsung.com/key/<alfanumérico>`, por lo que no se puede inyectar Markdown ni enlaces en el diálogo. |
| Dependencias | Una sola: `segno` (generador de QR en Python puro, sin dependencias y sin código de red), fijada a una versión exacta. |
| CI (GitHub Actions) | Acciones fijadas por hash de commit, permisos de sólo lectura y sin secretos. |

> Esta revisión la ha hecho quien escribió el código. Para una garantía
> independiente, pide a otra persona que revise el repositorio. El código es
> corto (unas 1.200 líneas) y está pensado para leerse.

## 2. ¿Quién podría ver la posición de tus tags?

### a) Samsung
Samsung ya recibe y guarda estas posiciones por usar SmartThings Find. La
integración no cambia eso. El **modo activo** sólo hace que se pidan
posiciones con más frecuencia.

### b) Quien obtenga la cookie de sesión (el riesgo principal)
La cookie `JSESSIONID` se guarda **en texto plano** en
`<config>/.storage/core.config_entries`, como el resto de credenciales de
Home Assistant. Quien la tenga puede hacer **todo lo que permite la web de
SmartThings Find** mientras siga siendo válida:

- ver la posición de todos tus dispositivos;
- hacerlos sonar;
- en móviles, tablets y relojes, usar las funciones remotas de la web
  (bloquear, borrar datos…).

Recomendaciones:
- **Copias de seguridad cifradas.** Desde 2025.1 Home Assistant cifra sus
  backups por defecto. Guarda la clave en sitio seguro y no subas backups sin
  cifrar a la nube.
- No compartas la carpeta `.storage` ni la incluyas en repositorios Git
  (cuidado con los "config backups" en GitHub).
- Si sospechas que se ha filtrado, **borrar la integración no invalida la
  cookie en los servidores de Samsung**. Cierra la sesión en
  https://smartthingsfind.samsung.com/ y/o cambia la contraseña de tu cuenta
  Samsung, y revisa en la cuenta los dispositivos con sesión iniciada.
- Esta integración **no** implementa "Descargar diagnósticos", así que esa
  función no puede filtrar la cookie ni las posiciones.

### c) Quien tenga acceso a tu Home Assistant
- **Todos los usuarios** de Home Assistant, no sólo los administradores, ven
  las entidades `device_tracker` en el mapa.
- El *recorder* guarda el **historial de posiciones** (10 días por defecto).
  Si no lo quieres:

  ```yaml
  recorder:
    exclude:
      entity_globs:
        - device_tracker.*   # o sólo los de tus tags
  ```

- Si Home Assistant es accesible desde Internet, usa contraseña robusta,
  **2FA** y Nabu Casa o un proxy con HTTPS. Revisa los *tokens de larga
  duración* que existan.
- No expongas los `device_tracker` a asistentes de voz ni a integraciones de
  terceros si no lo necesitas.

### d) El código QR del login
El login por QR puede usarse para phishing: alguien te enseña *su* QR y, si lo
apruebas, **él** obtiene acceso a tu cuenta. Aprueba el inicio de sesión en el
móvil **sólo si acabas de generar tú el QR** en tu Home Assistant. Si te llega
una solicitud que no esperabas, recházala.

### e) Rastreo físico por Bluetooth (independiente de esta integración)
Los SmartTag emiten continuamente anuncios BLE (con identificador rotatorio)
para que la red Galaxy los encuentre. Cualquiera con un escáner BLE cerca
puede detectar que *hay* un tag. Las funciones anti-acoso de Samsung
("búsqueda de etiquetas desconocidas") avisan si un tag ajeno te sigue. La
integración no modifica nada de esto.

### f) Ubicación cifrada
Si activas *"Cifrar ubicación sin conexión"* en la app SmartThings, las
posiciones de la red quedan cifradas extremo a extremo y la web, y por tanto
esta integración, **no las puede leer**. Es la opción más privada, a cambio
de que el tag deje de actualizarse en Home Assistant.

## Notificar un problema

Si encuentras un fallo de seguridad, no abras un issue público con detalles
explotables: contacta con el propietario del repositorio a través de GitHub.
