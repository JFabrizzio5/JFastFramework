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
| POST | `/auth/login` | `{email, password}` → un par de tokens |
| POST | `/auth/register` | Crea una cuenta e inicia sesión. Solo con `allow_registration = true` |
| GET | `/auth/account` | El usuario con sesión, con roles y permisos |
| POST | `/auth/password` | `{current_password, new_password}` |

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

---

## Contra qué protege

**Adivinar contraseñas.** Después de `max_failed_logins` contraseñas incorrectas
(5) la cuenta se bloquea `lockout_minutes` minutos (15). Se cuenta por cuenta y
no por dirección IP, así que cambiar de IP no sirve para saltárselo. Pon además
el plugin `ratelimit` delante de `/auth/login`, para frenar el intento de
adivinar en muchas cuentas a la vez.

**Averiguar quién tiene cuenta.** Una contraseña incorrecta, un email
desconocido y una cuenta bloqueada responden el mismo 401 con el mismo mensaje
-- y tardan lo mismo, porque un email desconocido también se verifica contra un
hash señuelo.

**Un permiso que sobrevive a su retiro.** `accounts` registra el hook
`on_refresh` de `auth`, así que cada refresh vuelve a leer los roles del
usuario. Quita un permiso, o desactiva al usuario, y aplica en su siguiente
refresh -- la desactivación termina la sesión ahí -- en vez de al final de un
refresh token de treinta días. El access token ya emitido sigue sirviendo hasta
que vence (`access_lifetime_minutes`, 15), y por eso conviene que sea corto.

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

Las tablas (`jfast_users`, `jfast_roles`, `jfast_role_permissions`,
`jfast_user_roles`) son del plugin: se crean al arrancar y el autogenerate de
Alembic del servicio las ignora.

## Lo que no está

- **Verificación de email y recuperación de contraseña por correo.** Las dos
  necesitan enviar correo y guardar un token; el plugin `mail` puede, y el flujo
  todavía no está construido.
- **Autenticación de varios factores.**
- **Permisos con comodín.** `invoices:*` no existe; enumera lo que un rol puede hacer.
