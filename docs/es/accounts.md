# Cuentas

Usuarios, login con contraseña, roles y permisos: el almacén de usuarios que el
plugin `auth` deja fuera a propósito, como un plugin que se activa.

```toml
[plugins]
enabled = ["observability", "database", "cache", "auth", "accounts"]

[plugin.auth]
mode = "secret"
algorithms = ["HS256"]
issue_tokens = true

[plugin.accounts]
bootstrap_admin_email = "admin@example.com"
```

```bash
pip install "jfastframework[accounts]"
JFAST_AUTH_SECRET=...                           # al menos 32 bytes
JFAST_ACCOUNTS_BOOTSTRAP_ADMIN_PASSWORD=...     # solo el primer arranque
```

En el primer arranque el plugin crea sus tablas, un rol llamado `admin` con el
permiso `accounts:admin`, y al administrador. Después quita la contraseña del
entorno: el usuario ya existe, y la variable solo se lee cuando no existe.

---

## El modelo

| Cosa | Qué es |
| --- | --- |
| Permiso | Un texto: `invoices:write`, `reports:read`. Los nombras tú. |
| Rol | Un conjunto con nombre de permisos: `billing` = `invoices:read` + `invoices:write`. |
| Usuario | Un email, una contraseña (o una identidad de un proveedor) y roles. |

Los permisos del usuario viajan en el access token como scopes, así que la
verificación en una ruta es una dependencia y ninguna consulta a la base:

```python
from fastapi import Depends
from jfastframework.accounts import require_permission
from jfastframework.auth import Principal

@router.post("/invoices")
async def create(caller: Principal = Depends(require_permission("invoices:write"))):
    ...
```

`require_permission` es `require_scopes` con el nombre que dice para qué sirve
aquí. Sin token es 401; con un token sin el permiso es 403.

---

## Endpoints

Bajo `prefix` (por defecto `/auth`), junto a `/auth/refresh` y `/auth/logout`
del propio `auth`:

| Método | Ruta | Hace |
| --- | --- | --- |
| POST | `/auth/login` | `{email, password}` → un par de tokens, o el segundo paso (abajo) |
| POST | `/auth/login/mfa` | `{mfa_token, code}` → un par de tokens. Con `mfa = true` |
| POST | `/auth/register` | Crea una cuenta. Solo con `allow_registration = true` |
| GET | `/auth/account` | El usuario con sesión, con roles, permisos, `email_verified` y `mfa_enabled` |
| POST | `/auth/password` | `{current_password, new_password}` |
| POST | `/auth/logout/all` | Termina todas las sesiones de quien llama, esta incluida |
| GET | `/auth/features` | Qué está activo: registro, verificación, reset, MFA -- para un frontend |
| POST | `/auth/verify` | `{token}` del correo. Con `email_verification` activo |
| POST | `/auth/verify/resend` | `{email}` → 202, siempre |
| POST | `/auth/password/forgot` | `{email}` → 202, siempre. Con `password_reset = true` |
| POST | `/auth/password/reset` | `{token, new_password}` → 204, y terminan todas las sesiones |
| POST | `/auth/mfa/setup` | Empieza la inscripción: `{secret, otpauth_uri}` |
| POST | `/auth/mfa/confirm` | `{code}` lo activa y devuelve los códigos de recuperación, una vez |
| POST | `/auth/mfa/disable` | `{password, code}` |
| POST | `/auth/mfa/recovery-codes` | `{password, code}` → un juego nuevo; los anteriores dejan de servir |

Los errores son RFC 7807 como en todo lo demás, y los que un frontend tiene que
distinguir traen un `code`: `email_not_verified`, `token_invalid`,
`mfa_code_invalid`, `mfa_token_invalid`, `password_invalid`,
`mfa_required_by_role`.

Bajo `admin_prefix` (por defecto `/accounts`), para quien tenga `accounts:admin`:

| Método | Ruta | Hace |
| --- | --- | --- |
| GET | `/accounts/users` | Usuarios, paginados |
| POST | `/accounts/users` | `{email, password?, display_name?, roles}` |
| PATCH | `/accounts/users/{id}` | `{is_active?, display_name?, roles?}` |
| GET | `/accounts/roles` | Roles y sus permisos |
| POST | `/accounts/roles` | `{name, description?, permissions}` |
| PATCH | `/accounts/roles/{id}` | `{description?, permissions?}` |
| DELETE | `/accounts/roles/{id}` | Quita el rol a todos los que lo tenían |
| DELETE | `/accounts/users/{id}/mfa` | Quita el MFA de un usuario: perdió el teléfono *y* los códigos |

`POST /accounts/users` acepta `email_verified: true` cuando el administrador
responde por la dirección; si no, con la verificación activa, al usuario le
llega el link. Desactivar a un usuario (`PATCH ... {is_active: false}`) termina
sus sesiones en ese momento -- el access token incluido, no cuando venza.

---

## Verificación de email

```toml
[plugins]
enabled = ["observability", "database", "cache", "queue", "auth", "mail", "accounts"]

[plugin.accounts]
allow_registration = true
email_verification = "required"          # off | optional | required
frontend_url = "https://app.example.com" # a dónde apunta el link del correo
```

`optional` manda el link al registrarse y la cuenta funciona mientras tanto;
`/auth/account` dice `email_verified: false` hasta que se use. `required` no da
sesión hasta entonces: el registro contesta **202** sin tokens, y un sign-in con
la contraseña correcta contesta **403** `email_not_verified` -- solo con la
contraseña correcta, así que la respuesta no le dice a nadie más que la cuenta
existe.

El link es `frontend_url` + `verify_email_path` (`/verify-email`) +
`?token=...`. El frontend postea el token a `/auth/verify`. El token son 256
bits aleatorios, **guardados solo como su SHA-256**, válidos
`verification_token_minutes` (un día) y de un solo uso: consumirlo es un
`UPDATE` condicional, así que dos clics en el mismo link dan un solo éxito. Un
reenvío deja funcionando el primer link -- el primer correo suele ser el que se
abre.

El correo sale por el plugin `mail`, encolado cuando `queue` está activo. Los
tres mensajes -- `accounts/verify_email`, `accounts/reset_password`,
`accounts/already_registered` -- tienen versiones sencillas integradas; pon
`<nombre>.html` (y `.txt`) en el `templates_dir` de mail para reemplazar uno.
Reciben `app_name`, `link`, `email`, `display_name` y `expires_minutes`.

**Registrarse con una dirección ocupada**, en modo `required`, contesta el mismo
202 que una nueva, y al dueño de la dirección le llega un correo que lo dice,
con un link de reset. El formulario nunca dice "ese email ya existe". En `off` y
`optional` el registro inicia sesión, así que una dirección ocupada es un 409,
como antes: no hay forma de entregar una sesión y ocultarlo.

Los administradores y el admin de arranque quedan verificados al crearse (el
operador escribió la dirección), un email verificado por un proveedor cuenta, y
completar un reset de contraseña también -- el link llegó al buzón.

Activar la verificación en un servicio con usuarios: todas las cuentas que ya
existían quedan sin verificar. Con `required` reciben el 403 y pueden pedir un
link. Para tratar como verificadas las cuentas anteriores, corre una vez:
`UPDATE jfast_users SET email_verified_at = created_at WHERE email_verified_at IS NULL`.

## Recuperar la contraseña

```toml
[plugin.accounts]
password_reset = true
frontend_url = "https://app.example.com"
```

`POST /auth/password/forgot {email}` contesta **202, sea cual sea la
dirección** -- y en el mismo momento: la búsqueda, el token y el correo pasan
después de enviada la respuesta, así que el tiempo de respuesta no dice si la
dirección tiene cuenta. El link es `reset_password_path` (`/reset-password`),
válido `reset_token_minutes` (30), de un solo uso, hasheado en reposo.

`POST /auth/password/reset {token, new_password}` revisa la política de
contraseña *antes* de gastar el token (una contraseña demasiado corta no cuesta
el link), fija la contraseña, quita un bloqueo y **termina todas las sesiones de
la cuenta**: cada familia de refresh registrada al iniciar sesión se revoca en
el store de `auth`, lo que también detiene los access tokens que la llevan. No
inicia sesión: eso se saltaría el segundo factor.

Como mucho un correo por dirección cada `email_cooldown_seconds` (60), por más
veces que se envíe el formulario.

## Autenticación de dos factores

```toml
[plugin.accounts]
mfa = true
mfa_required_roles = ["admin"]    # opcional
```

```bash
JFAST_ENCRYPTION_KEYS=k1:...      # ver encryption.md; obligatoria con mfa = true
```

TOTP (RFC 6238): seis dígitos, treinta segundos, SHA-1 -- lo que lee cualquier
app autenticadora. Escrito con la biblioteca estándar (`hmac`, `hashlib`,
`base64`), sin dependencia nueva, y probado contra los vectores del propio RFC.

**La inscripción** tiene dos pasos. `POST /auth/mfa/setup` (con la contraseña
actual) guarda un secreto nuevo y lo devuelve con una URI `otpauth://`; todavía
no protege nada. `POST /auth/mfa/confirm {code}` lo activa con un código que
calculó la app, y devuelve diez **códigos de recuperación** -- se muestran una
vez, se guardan como SHA-256 y cada uno sirve para un sign-in.

**Iniciar sesión** pasa a tener dos pasos:

```
POST /auth/login       {email, password}   -> {"mfa_required": true, "mfa_token": "...", "expires_in": 300}
POST /auth/login/mfa   {mfa_token, code}   -> el par de tokens
```

`code` son los seis dígitos o un código de recuperación. El token MFA es de un
solo uso y dura `mfa_token_minutes` (5). Se acepta un código un paso antes o
después del actual (un teléfono con treinta segundos de desfase sigue
funcionando); **el mismo código nunca se acepta dos veces**, ni uno anterior --
se guarda el último paso aceptado y se avanza con un `UPDATE` condicional, así
que dos requests que compiten con un código obtienen un solo sí. Un código
equivocado cuenta contra el token MFA (`mfa_max_attempts`, 5, y luego a iniciar
sesión otra vez) *y* contra el bloqueo de la cuenta, así que saber la contraseña
no compra intentos extra con el código: el contador de fallos solo se limpia
cuando de verdad se emite una sesión.

**Por rol.** Un usuario con un rol de `mfa_required_roles` y sin MFA va a la
inscripción al iniciar sesión -- `{"mfa_enrollment_required": true,
"mfa_token": ...}`; `/auth/mfa/setup` y `/confirm` aceptan ese token en el body
en lugar de una sesión, y `confirm` devuelve entonces la sesión junto con los
códigos de recuperación. Una sesión que gana ese rol termina en su siguiente
refresh, y el MFA no se puede quitar mientras se tenga el rol.

El secreto es la única credencial que no se puede hashear -- el servidor calcula
el código a partir de él -- así que se guarda **cifrado** con
`JFAST_ENCRYPTION_KEYS`, atado a su usuario. El plugin no arranca con
`mfa = true` y sin llave.

Un sign-in por proveedor (login social de `auth`) de una cuenta con MFA devuelve
el mismo reto `mfa_required` en vez de tokens: un proveedor es el primer factor.

## Límites de frecuencia

Con el plugin `cache` activo, el sign-in queda limitado sin configurar nada,
además del bloqueo:

| Cubeta | Por defecto | Aplica a |
| --- | --- | --- |
| por dirección IP | 20 cada 5 min (`login_limit_per_ip`) | login, el paso MFA, verify, reset |
| por cuenta | 10 cada 5 min (`login_limit_per_account`) | login (según el email escrito), el paso MFA |
| correos por IP | 5 cada 15 min (`email_limit_per_ip`) | registro, reenvío, forgot |

Un token bucket en Redis -- el limitador del propio plugin `ratelimit`, sobre la
conexión del cache; no hace falta activar el plugin `ratelimit`. Pasarse es un
429 con `Retry-After`. La cubeta por cuenta usa lo que se escribió, así que
contesta igual para una dirección sin cuenta. Si Redis deja de contestar, deja
pasar y el bloqueo sigue aplicando. `rate_limit = false` lo apaga; sin `cache`
está apagado y lo dice al arrancar.

---

## Contra qué protege

**Adivinar contraseñas.** Después de `max_failed_logins` contraseñas o códigos
incorrectos (5) la cuenta se bloquea `lockout_minutes` minutos (15). Se cuenta
por cuenta y no por dirección IP, así que cambiar de IP no sirve para
saltárselo; los límites de arriba frenan el intento de adivinar en muchas
cuentas a la vez.

**Averiguar quién tiene cuenta.** Una contraseña incorrecta, un email
desconocido y una cuenta bloqueada responden el mismo 401 con el mismo mensaje
-- y tardan lo mismo, porque un email desconocido también se verifica contra un
hash señuelo. "Mándame un link" (reset, reenvío) contesta 202 antes de buscar la
dirección, y el registro con verificación obligatoria contesta igual para una
dirección ocupada.

**Un permiso que sobrevive a su retiro.** `accounts` registra el hook
`on_refresh` de `auth`, así que cada refresh vuelve a leer los roles del
usuario. Quita un permiso, o desactiva al usuario, y aplica en su siguiente
refresh -- la desactivación termina la sesión ahí -- en vez de al final de un
refresh token de treinta días. El access token ya emitido sigue sirviendo hasta
que vence (`access_lifetime_minutes`, 15), y por eso conviene que sea corto --
salvo al desactivar, al hacer un reset de contraseña y con `/auth/logout/all`,
que revocan todas las sesiones en ese momento, access tokens incluidos.

**Una tabla de tokens robada.** Los tokens de verificación y de reset y los
códigos de recuperación se guardan como SHA-256; el secreto TOTP, cifrado. Una
copia de la base no resetea la contraseña de nadie. Ninguno se escribe jamás en
el log.

**Una tabla de contraseñas robada.** Las contraseñas se hashean con argon2id,
fuera del event loop, y se vuelven a hashear en el siguiente login cuando cambian
los parámetros.

**Tomar una cuenta a través de un proveedor.** Con login social configurado en
`auth`, una identidad de proveedor se enlaza a una cuenta existente solo por un
email *verificado*. Uno sin verificar dejaría entrar como dueño a cualquiera que
pueda escribir esa dirección en un proveedor.

---

## Tenants

Cada usuario y cada rol pertenece a un tenant, o a ninguno. El login, el
registro y el login social leen el tenant de la request -- la fuente de
subdominio o de ruta del plugin `tenancy`; la del token no puede, porque todavía
no hay token. El mismo email en dos tenants son dos personas, y un
administrador ve y cambia solo los usuarios y roles de su propio tenant.

**Un SaaS donde cada cuenta es su propio tenant** no necesita tabla de
tenants: agrega el plugin tenancy con `sources = ["token", "user"]` y el id del
usuario con sesión se vuelve el tenant de todo lo que crea. Los usuarios en sí
no pertenecen a ningún tenant, y es lo correcto: la cuenta es la frontera. Ver
[la fuente `user`](multitenancy.md#cada-cuenta-es-su-propio-tenant-la-fuente-user).

---

## Configuración

| Setting | Por defecto | |
| --- | --- | --- |
| `prefix` / `admin_prefix` | `/auth` / `/accounts` | |
| `allow_registration` | `false` | También decide si el login social puede crear usuarios |
| `default_roles` | `[]` | Roles con los que empieza un usuario que se registra solo |
| `min_password_length` | `10` | |
| `max_failed_logins` / `lockout_minutes` | `5` / `15` | |
| `admin_permission` | `accounts:admin` | |
| `bootstrap_admin_email` | `""` | Contraseña desde `JFAST_ACCOUNTS_BOOTSTRAP_ADMIN_PASSWORD` |
| `social_login` | `true` | Solo actúa si `auth` tiene proveedores |
| `email_verification` | `off` | `off`, `optional` o `required`. Necesita `mail` |
| `verification_token_minutes` | `1440` | |
| `password_reset` | `false` | Necesita `mail` |
| `reset_token_minutes` | `30` | |
| `frontend_url` | `""` | Obligatorio si cualquiera de los dos de arriba está activo (`JFAST_ACCOUNTS_FRONTEND_URL`) |
| `verify_email_path` / `reset_password_path` / `login_path` | `/verify-email` / `/reset-password` / `/login` | Las rutas del frontend a las que apuntan los links |
| `email_cooldown_seconds` | `60` | Un correo de cada tipo por dirección en ese lapso |
| `mfa` | `false` | Necesita `JFAST_ENCRYPTION_KEYS` |
| `mfa_required_roles` | `[]` | Necesita `mfa = true` |
| `mfa_issuer` | el nombre de la app | Lo que muestra la app autenticadora |
| `mfa_token_minutes` / `mfa_max_attempts` | `5` / `5` | El segundo paso de un sign-in |
| `recovery_codes` | `10` | |
| `rate_limit` | `true` | Solo actúa con el plugin `cache` |
| `login_limit_per_ip` / `login_limit_per_account` / `login_window_seconds` | `20` / `10` / `300` | |
| `email_limit_per_ip` / `email_window_seconds` | `5` / `900` | |

Cada combinación que no puede funcionar detiene el servicio al arrancar,
nombrando el arreglo: verificación o reset sin `mail` o sin `frontend_url`,
`mfa_required_roles` sin `mfa`, `mfa` sin llave de cifrado.

Las tablas (`jfast_users`, `jfast_roles`, `jfast_role_permissions`,
`jfast_user_roles`, `jfast_account_tokens`, `jfast_recovery_codes`,
`jfast_user_sessions`) son del plugin: se crean al arrancar y el autogenerate de
Alembic del servicio las ignora. Una `jfast_users` creada por una versión
anterior también recibe sus columnas nuevas al arrancar (todas nullable, así que
el `ALTER` es instantáneo): no hay migración que escribir.

## Lo que no está

- **Permisos con comodín.** `invoices:*` no existe; enumera lo que un rol puede hacer.
- **Passkeys (WebAuthn), SMS o códigos por correo.** El segundo factor es TOTP.
- **Un código QR.** `/auth/mfa/setup` devuelve la URI `otpauth://`; dibujarla es
  decisión del frontend (el generado muestra el link y la clave).
- **Cambiar la contraseña no termina las otras sesiones.** Un reset sí, y
  también `/auth/logout/all`.
- **El paso MFA después de un sign-in por proveedor, en el frontend generado.**
  El backend devuelve el reto; el callback de proveedor del frontend todavía no
  lo maneja.
