# SmartThings Find para Home Assistant

Integración personalizada (`custom_component`) que muestra en el **mapa de Home
Assistant** la posición de los **Samsung Galaxy SmartTag** (v1, SmartTag+ y
SmartTag2) y del resto de dispositivos de tu cuenta en
[SmartThings Find](https://smartthingsfind.samsung.com/).

> ⚠️ **API no oficial.** Samsung no ofrece una API pública para la ubicación de
> los SmartTag. Esta integración usa los mismos endpoints que la web de
> SmartThings Find (obtenidos por ingeniería inversa), así que puede dejar de
> funcionar si Samsung cambia la web.

## ¿Es viable la integración? — Análisis

| Opción | ¿Da la ubicación de los SmartTag? | Comentario |
|---|---|---|
| Integración oficial **SmartThings** de HA (API pública `api.smartthings.com`) | ❌ No | Los SmartTag aparecen (a veces) como dispositivos, pero la API pública **no expone su posición**. Sólo sirve para el botón del tag. |
| **Web de SmartThings Find** (`smartthingsfind.samsung.com`) | ✅ Sí | Devuelve la última posición conocida, precisión, fecha y batería de cada dispositivo. Requiere sesión de Samsung Account. **Es la que usa esta integración.** |
| Escaneo BLE propio (ESPHome / Bluetooth proxy) | ⚠️ Parcial | Sólo detecta presencia/RSSI cerca de tus receptores (útil para "¿está en casa?"), no posición GPS. Los tags rotan su dirección MAC, lo que complica identificarlos. |

**Conclusión:** sí se puede, y la vía práctica es la web de SmartThings Find.

### Cómo funciona

1. **Login**: se reproduce el inicio de sesión con **código QR** de Samsung
   Account (`account.samsung.com/accounts/v1/FMM2/...`). Escaneas el QR con tu
   móvil, apruebas el acceso y la integración obtiene la cookie de sesión
   `JSESSIONID` de SmartThings Find. **Tu contraseña nunca pasa por Home
   Assistant.**
2. **Token CSRF**: `GET /chkLogin.do` devuelve la cabecera `_csrf` que se
   adjunta a cada petición.
3. **Dispositivos**: `POST /device/getDeviceList.do`.
4. **Posición/batería**: `POST /device/setLastSelect.do` devuelve una lista de
   "operaciones" (`LOCATION`, `LASTLOC`, `OFFLINE_LOC`, `CHECK_CONNECTION`…).
   Se escoge la posición **más reciente no cifrada**.
5. **Modo activo** (opcional): `POST /dm/addOperation.do` con
   `CHECK_CONNECTION_WITH_LOCATION` pide a la red que localice el tag; la nueva
   posición se lee en la siguiente consulta.
6. **Sonar**: `POST /dm/addOperation.do` con `RING`.

### Limitaciones a tener en cuenta

- **La posición de un SmartTag no es GPS del tag**: el tag no tiene GPS. Es la
  posición del Galaxy (tuyo o de cualquier otro usuario de la red Find) que lo
  detectó por BLE/UWB, con la precisión de ese móvil. La frecuencia de
  actualización depende de que haya Galaxys cerca.
- **SmartTag v1 vs SmartTag2**: la API los trata igual (`deviceTypeCode =
  TAG`); el SmartTag+ y el SmartTag2 tienen UWB, pero esa búsqueda de precisión
  sólo funciona en el móvil, no en la web.
- **Ubicación cifrada**: si en la app SmartThings activaste *"Cifrar ubicación
  sin conexión"* para un tag, esas posiciones llegan cifradas y **no pueden
  leerse** desde la web; la entidad tendrá el atributo `encrypted_location:
  true` y se quedará con la última posición en claro.
- **Caducidad de la sesión**: no se sabe cuánto dura la cookie. Si caduca,
  Home Assistant mostrará un aviso de *re-autenticación* para volver a escanear
  el QR.
- **Sonar** sólo funciona si un Galaxy de tu cuenta está cerca del tag (lo
  reenvía por Bluetooth). Si no suena desde la web de SmartThings Find,
  tampoco sonará desde Home Assistant.
- Los botones físicos del tag **no** se gestionan aquí (usa la integración
  oficial de SmartThings para eso).

## Entidades que crea

Por cada dispositivo de la cuenta:

| Entidad | Descripción |
|---|---|
| `device_tracker.<nombre>` | Posición en el mapa (lat/lon, `gps_accuracy`, `battery_level`, `last_seen`, `location_source`). |
| `sensor.<nombre>_battery` | Batería. En los tags es aproximada: `FULL`=100, `MEDIUM`=50, `LOW`=15, `VERY_LOW`=5 (el valor original está en `raw_level`). |
| `sensor.<nombre>_last_seen` | Fecha/hora (UTC) de la última posición conocida. |
| `button.<nombre>_ring` | Hace sonar el dispositivo. |

## Instalación

### HACS (repositorio personalizado)

1. HACS → menú ⋮ → *Repositorios personalizados* → añade
   `https://github.com/sgbeitia/ha-smarthings-find` con categoría
   **Integración**.
2. Instala **SmartThings Find** y reinicia Home Assistant.

### Manual

Copia `custom_components/smartthings_find` en
`<config>/custom_components/` y reinicia Home Assistant.

## Configuración

1. *Ajustes → Dispositivos y servicios → Añadir integración* → busca
   **SmartThings Find** (no confundir con la integración oficial *SmartThings*).
2. Pulsa *Enviar*: aparecerá un **código QR**. Escanéalo con la cámara de un
   móvil con sesión iniciada en tu cuenta Samsung y aprueba el acceso
   (caduca en 3 minutos).
3. Se crearán los dispositivos y entidades automáticamente.

### Opciones

En *Configurar* sobre la integración:

- **Intervalo de actualización** (30–3600 s, por defecto 120 s).
- **Modo activo para SmartTags** (por defecto activado): pide una posición
  nueva en cada consulta.
- **Modo activo para otros dispositivos** (por defecto desactivado): en
  móviles/relojes gasta batería y puede encender la pantalla.

### Ejemplo: tarjeta de mapa

```yaml
type: map
entities:
  - device_tracker.llaves
  - device_tracker.mochila
hours_to_show: 24
```

## Depuración

```yaml
logger:
  default: info
  logs:
    custom_components.smartthings_find: debug
```

## Desarrollo

```bash
pip install -r requirements_test.txt
pytest
```

Los tests incluyen un servidor Samsung simulado que reproduce el flujo QR
completo y la renovación del token CSRF, además de tests de la integración
dentro de Home Assistant (`pytest-homeassistant-custom-component`).

## Créditos

Basada en el trabajo de ingeniería inversa de
[Vedeneb/HA-SmartThings-Find](https://github.com/Vedeneb/HA-SmartThings-Find)
(MIT, archivado). Esta es una reescritura con sesión HTTP aislada, cliente
independiente de Home Assistant, renovación automática del CSRF,
re-autenticación y tests.
