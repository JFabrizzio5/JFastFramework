# Frontends

Dos maneras de poner una UI delante de un backend JFast. Elige según si
quieres un paso de build o no.

| | `--kind web` | `--kind spa` |
| --- | --- | --- |
| Renderiza | Jinja2 + HTMX del lado del servidor | Vue 3 o React, del lado del cliente |
| Paso de build | ninguno | Vite |
| Vive | dentro del servicio backend | en su propio servicio |
| Bueno para | paneles de administración, herramientas internas, CRUD | estado de cliente rico, offline, UX tipo móvil |

`--kind web` está cubierto en [modules.md](modules.md). Esta página trata de
`--kind spa`.

---

## Crear uno

```bash
jfast new service admin --kind spa --frontend vue
cd admin
npm install
npm run dev
```

React en su lugar:

```bash
jfast new service portal --kind spa --frontend react
```

Angular **no** se genera. Ver "Angular" al final.

## Elige un look: `--template`

Un frontend se genera en uno de dos looks. La elección solo cambia los
archivos que dibujan: la hoja de estilos, los componentes base, el layout, las
dos pantallas incluidas y la página que escribe `jfast new view`. Router,
stores, la instancia de axios, el manejo del 401 y los marcadores del
generador son un solo conjunto compartido, así que todo lo demás de esta
página vale para los dos.

| `--template` | Cómo se ve |
| --- | --- |
| `nexora` **(por defecto)** | Vidrio líquido, del sistema de diseño Nexora: paneles esmerilados sobre un estudio negro (y uno claro), barra lateral de vidrio, isla superior flotante, Plus Jakarta Sans con JetBrains Mono para las cifras, y un listón líquido en WebGL detrás de todo. La página de inicio es un pequeño dashboard — tarjetas KPI, una tabla de vistas registradas, una columna de siguientes pasos — hecho solo con valores reales: `/health`, su tiempo de ida y vuelta, y la barra lateral |
| `classic` | Paneles de Tailwind v4 sobre una superficie neutra con un solo acento carmesí. Sin fuentes web, sin WebGL. Exactamente como se veía todo frontend antes de que existieran los looks |

```bash
jfast new service admin --kind spa --frontend vue                     # nexora
jfast new service admin --kind spa --frontend react --template classic
jfast start shop --template classic                                   # el stack por defecto
jfast init                                                            # pregunta "Which look?"
```

Un valor desconocido se rechaza antes de escribir nada, nombrando los dos que
existen. `--template` en un servicio que no es `--kind spa` también se
rechaza: solo un frontend tiene look.

### Las vistas siguen al look

La elección se registra una vez, en el sello `.jfast-template` del proyecto
(`frontend_template`). `jfast new view` lo lee, así que una página generada un
año después se dibuja como las pantallas que la rodean. `--template` en
`jfast new view` se impone al sello; un proyecto sin sello — hecho antes de
los sellos, o no por jfast — recibe la página classic y una nota que lo dice,
porque una página nexora nombraría clases que ese proyecto no tiene.

### Lo que agrega Nexora

| Ruta | Contiene |
| --- | --- |
| `src/nexora/nexora.css` | El sistema de diseño: la paleta, los tokens claro y oscuro, y las clases `card-panel`, `btn-modern`, `badge-status`, `kpi-card`, `erp-table`, `nx-*`. Se importa en la capa `components` de Tailwind, así que una utilidad junto a una de sus clases sigue ganando |
| `src/nexora/accent.js` | El acento: seis presets, la paleta entera derivada de un color, y recordar lo que eligió quien mira |
| `src/nexora/brand.js` | El nombre del producto en el logotipo |
| `src/nexora/background.js` | Si el listón corre o no |
| `src/nexora/backdrop.js` | El fondo que eligió quien mira: 3D, 2D o nada |
| `src/nexora/sidebar.js` | Si el sidebar está plegado en pantalla ancha |
| `src/nexora/liquid.js` | El listón, sobre three.js |
| `src/components/AccentPicker.*`, `LiquidBackground.*` | Los dos componentes que el look classic no tiene |

**El listón no cuesta nada cuando no puede aportar.** `three` es una
dependencia de npm, nunca un script de CDN, y va en un chunk propio (unos
130 kB con gzip) que se descarga después del primer pintado. Con
`prefers-reduced-motion` nunca se carga, y activar esa preferencia lo detiene.
Sin WebGL 2 queda en pantalla el cuadro fijo de CSS que siempre está debajo.
Una pestaña oculta no dibuja nada. Se monta una sola vez en la raíz de la app,
no en el layout, así que navegar no reconstruye la escena.

**El nombre** es `VITE_APP_NAME`, tal cual está escrito, o `jfastframework`
cuando está vacío. Se cambia en `.env` para `npm run dev` y en
`.env.production` para `npm run build`. `jfast workspace env` reescribe
`.env`, así que hay que volver a ponerlo ahí después de correrlo.

**El acento** es un solo color, y de él se derivan el brillo, el tono profundo,
el texto de los botones y dos tonos seguros para texto — movidos justo lo
necesario para leerse a 4.5:1 en cada tema, así que un acento ámbar o pizarra
no produce etiquetas ilegibles. Tres lugares lo fijan, gana el primero:

1. quien mira, con la muestra junto al botón de tema: Ruby, Blue, Emerald,
   Violet, Amber, Slate o cualquier color propio, guardado en ese navegador
   bajo `<service>:accent` y reaplicado antes del primer pintado por
   `index.html`;
2. `VITE_ACCENT="#3B82F6"` en `.env` / `.env.production` — entre comillas,
   porque un `#` sin comillas empieza un comentario en esos archivos;
3. los tripletes `--c-*` al inicio de `src/nexora/nexora.css`.

Botones, brillos, badges, toda utilidad `brand-*` y el listón lo siguen.

**El fondo** se elige en el mismo popover que el acento: **3D** es el listón,
**2D** el cuadro fijo que tiene debajo -- y three.js nunca se descarga -- y
**Off** el color liso de la página. Cambiar de 3D a otro destruye la escena que
corre; regresar la carga. `VITE_BACKGROUND=liquid|still|none` en `.env` /
`.env.production` es el default del proyecto, `liquid` si está vacío; la
elección de quien mira, guardada bajo `<service>:backdrop`, gana sobre él. Los
dos los aplica `index.html` antes del primer pintado, así que "Off" no muestra
un instante el cuadro fijo al recargar.

**El botón de menú** abre el drawer debajo de 960px y, arriba, pliega el
sidebar para que la página use todo el ancho. Plegado se queda plegado entre
páginas y recargas (`<service>:sidebar`), y un sidebar plegado queda fuera del
foco del teclado, no solo fuera de la pantalla.

### JFast Suite, como referencia

Con `--agent-docs`, un frontend nexora también recibe la skill
`nexora-reference`: una copia de JFast Suite -- las páginas estáticas de las que
salió el look: una landing, una consola CRM, una pasarela de pagos, un feed
social, tablas de operación, una galería de widgets -- en
`.jfast/skills/nexora-reference/suite/`. Sirve esa carpeta y ábrela antes de
construir una pantalla que el proyecto todavía no tiene. La skill dice cómo
traer un patrón: reusar un componente incluido si alguno sirve, mover el CSS a
`src/nexora/nexora.css` con los tokens del proyecto, y hacerlo componente. La
suite nunca se enlaza desde la app, y sus imágenes (unos 400 kB en WebP) son
arte de demostración.

### Pedir otro look

El look incluido es un punto de partida. Para el otro, genera con
`--template`; para un look que jfast no incluye, reestiliza los tokens y los
componentes de `src/components/`, conservando sus props, porque las páginas
generadas los usan. Un agente que trabaje en el proyecto recibe la misma
instrucción: la skill de diseño que escribe `--agent-docs` describe el look
con el que se generó el proyecto, y abre diciendo que el look que pide el
usuario gana sobre él.

## La URL de la API ya está bien

`.env` se escribe a partir de `jfast.workspace.toml`:

```
VITE_API_URL=http://localhost:8030
```

Eso apunta al gateway cuando el workspace tiene uno, y al único backend cuando
no. La home page generada llama a `/health` a través de él en la primera carga
y muestra el resultado, así que un valor equivocado aparece de inmediato en vez
de en tu primera feature real.

Después de agregar un backend (o el día que aparezca un gateway):

```bash
jfast workspace env
```

---

## Agregar un módulo

Esta es la parte modelada sobre el generador que ya tenías:

```bash
jfast new view Facturas
```

```
src/ModuloFacturas/
├── Components/
│   ├── Modals/
│   └── Tables/
├── Pages/FacturasView.vue
├── Routes/router.js
└── Services/facturas.service.js
```

y después registra el módulo en dos lugares:

```js
// src/router/index.js
import { ModuloFacturas } from '@/ModuloFacturas/Routes/router.js'
...
  ...ModuloFacturas,
  /*nuevaRuta*/
```

```js
// src/menuAside.js
import { mdiHomeOutline, mdiViewDashboardOutline } from '@mdi/js'
...
  {
    to: '/facturas',
    icon: mdiViewDashboardOutline,
    label: 'Facturas',
  },
  /*nuevoModulo*/
```

El framework se detecta desde el proyecto, así que no repites
`--frontend react` dentro de un proyecto React. El look también: la página se
dibuja en nexora o classic según el `.jfast-template` del proyecto (ver
[Las vistas siguen al look](#las-vistas-siguen-al-look)).

### Los marcadores

Conserva `/*nuevaRuta*/` y `/*nuevoModulo*/`. Tres propiedades están
garantizadas, y cada una es un modo de falla que de otro modo es silencioso:

| Propiedad | Sin ella |
| --- | --- |
| **Idempotente** — primero se revisa una cadena de guarda | Dos rutas y dos entradas de sidebar por cada re-ejecución |
| **Ruidoso** — un archivo o marcador faltante lanza error con la ruta | Una página en blanco y ninguna explicación |
| **Preserva el marcador** — el marcador se vuelve a escribir después del bloque | El segundo módulo no tiene a dónde ir |

Un marcador reformateado (`/* nuevaRuta */`) igual coincide: hacer el match
estricto convertiría una corrida de `prettier` en un no-op silencioso.

### Nombres

`Facturas` → `ModuloFacturas`, `/facturas`, `facturas.service.js`.
`BillingAccount` → `ModuloBillingAccount`, `/billing-account`, y la etiqueta
del sidebar dice `Billing Account`.

---

## Componentes base

Ambos frontends traen el mismo set, así que los dos son el mismo producto en
vez de dos proyectos que casualmente comparten un backend.

| Componente | Qué resuelve por ti |
| --- | --- |
| `BaseButton` | `primary` / `outline` / `ghost` / `danger`, dos tamaños, y un estado de carga que además lo deshabilita |
| `BaseInput` | Label, texto de error, hint, disabled — está pensado para que sea imposible mandar a producción un input pelado sin label |
| `BaseModal` | Blur del fondo, Escape y clic en el fondo, foco atrapado mientras está abierto y devuelto al disparador al cerrar |
| `BaseBadge` | Colores de estado, con la palabra siempre visible |
| `SkeletonLoader` | La forma del contenido que viene en camino |
| `EmptyState` | Un encabezado, una frase, y la acción que crea el primero |
| `ToastHost` | Renderiza la cola de toasts; se monta una vez en la raíz de la app |

**Un botón destructivo va outlined, nunca sólido.** El rojo sólido ya está
tomado por "continuar", y una pantalla donde el acento significa dos cosas
opuestas no tiene acento.

**El drawer móvil bloquea el scroll del body**, igual que `BaseModal` y por la
misma razón: cubre la página, así que un swipe destinado al menú si no scrollea
el artículo de abajo y se lleva el menú con él. Restaura el valor previo en vez
de limpiarlo, así que un modal que ya tenía el lock lo conserva, y se cierra
solo pasado `md` — donde el sidebar es estático, no hay nada que cerrar y el
lock sería nada más una página que no scrollea.

### Los dos estados que la gente olvida

**Cargando no es la palabra "Cargando".** `SkeletonLoader` recibe props para
líneas y forma, así que se le puede dar la forma de lo que está llegando — que
es lo que evita que el layout salte cuando llega.

**Vacío no es una página vacía.** `EmptyState` dice qué estaría ahí y ofrece la
acción que lo crea. Una tabla en blanco se lee como un bug.

### Toasts

```js
// Vue
import { useToast } from '@/composables/useToast.js'
const toast = useToast()
toast.success('Invoice created')

// React
import useToast from '@/hooks/useToast.js'
const toasts = useToast((state) => state.toasts)

// Anywhere that cannot call a hook — an interceptor, a loader, a guard:
import { toast } from '@/stores/notification.store.js'
toast.error('Could not reach the server')
```

Un store, dos puertas. Un toast lanzado desde el fondo de un service y uno
lanzado en un componente caen en la misma lista, en orden, bajo los mismos
timers. Dos stores significarían que un toast lanzado desde un service va a una
lista que el host no renderiza — nada en pantalla, y tampoco un error.

Los timers viven fuera del store y se limpian al descartar manualmente. Un
toast descartado igual tiene un timeout apuntándole; si se lo deja vivo, cada
descarte manual filtra uno que después escribe estado para un toast que ya no
existe.

---

## Estado

| | Vue | React |
| --- | --- | --- |
| Librería | Pinia | Zustand |
| Auth | `src/stores/auth.store.js` | misma ruta |
| Notificaciones | `src/stores/notification.store.js` | misma ruta |

Ambos vienen cableados: Pinia está instalado en la app, Zustand está en
`package.json`, y el store de auth persiste su token de la misma forma en que
`services/api.js` ya lo lee — una sola fuente de verdad, nombrada en un
comentario en el archivo.

**Cuándo no usar un store.** El estado que ningún otro componente lee pertenece
al componente. Un store es para algo que necesitan dos lugares no relacionados:
el layout lee el usuario, axios necesita el token. Eso es dos, así que es un
store.

---

## Sesiones, y qué pasa en un 401

Los access tokens duran poco. Ambos frontends traen las dos mitades que hacen
falta:

| | Dónde | Cubre |
| --- | --- | --- |
| Guard de rutas | `src/router/index.js` / `index.jsx` | navegar a una página sin sesión |
| Interceptor de 401 | `src/services/api.js` | el token venciendo con la página ya abierta |

Un guard solo no alcanza. Corre en la navegación, y nada navega mientras cuatro
paneles de la pantalla actual fallan en silencio.

**Las rutas son privadas salvo que digan lo contrario.** `PUBLIC_BY_DEFAULT`,
arriba del router, es la única línea que cambiar, y viene en `false`: el
backend inicia sesión con el plugin `accounts` ([accounts.md](accounts.md)),
así que cada página queda detrás del sign-in, incluidas las que `jfast new view`
agregue después. Una página para todo el mundo se sale con
`meta: { public: true }` (Vue) o `handle: { public: true }` (React). El guard de
React envuelve la lista entera de rutas en vez de cada ruta, así que un módulo
agregado en `/*nuevaRuta*/` queda cubierto sin que el generador sepa nada de
autenticación.

Un frontend generado para un workspace sin backend con `accounts`
(`frontend_accounts = false`, abajo) conserva el default de antes: público, con
rutas que se protegen con `requiresAuth`, y sin las páginas de cuenta -- un
formulario de sign-in delante de todo que nada puede satisfacer sería peor.

`LOGIN_ROUTE` se exporta desde `auth.store.js`, porque `api.js` también lo
necesita y una segunda copia de `'/login'` es un segundo lugar que olvidar.

### El interceptor, y el bug que tiene la versión obvia

Ante un 401 refresca una vez y reenvía el request. Tres cosas ahí no son
opcionales:

**Un refresh para toda la ráfaga.** Cuatro paneles cargando juntos producen
cuatro 401. Cuatro refreshes presentan el mismo refresh token cuatro veces —
rotan, así que tres de esos están gastados, y un backend que lee el replay como
robo revoca la sesión. `refreshOnce()` le entrega a cada llamador la misma
promise.

**Un refresh que a su vez es rechazado termina la sesión.** Limpia el store y
hace una navegación de página completa a `LOGIN_ROUTE` con `?next=`, una sola
vez, sin importar cuántos requests fallaron juntos. Reintentar un refresh
rechazado es el loop.

**Al request reenviado hay que ponerle el token nuevo, y esta es la parte que
parece que ya funciona.** Refrescar actualiza el lugar de donde el token sale
normalmente — `api.defaults.headers` en Vue. Los defaults no llegan a un config
que ya carga `Authorization`, y el config reenviado carga uno: axios mezcló el
token viejo cuando el request se armó la primera vez. Entonces el refresh
devuelve 200, los reintentos vuelven a salir con el token que acaba de vencer, y
la app se renderiza como logueada con todos los paneles vacíos:

```
/invoices      401  Bearer A1
/clients       401  Bearer A1
/projects      401  Bearer A1
/notifications 401  Bearer A1
refresh        200  -> A2
/invoices      401  Bearer A1     <-- refreshed, retried, same dead token
/clients       401  Bearer A1
/projects      401  Bearer A1
/notifications 401  Bearer A1
```

Vue lo arregla poniendo `original.headers.Authorization` antes del reenvío.
React no necesita esa línea, y por una razón que vale conocer en vez de por
suerte: su interceptor de request pone el header por request desde
`localStorage`, y el reenvío vuelve a pasar por ahí. Eso se sostiene solo
mientras la asignación siga siendo incondicional — envuélvela en
`if (!config.headers.Authorization)` y React tiene el bug idéntico.

### Las páginas de cuenta

Lo que ofrece el plugin `accounts` tiene su página en el frontend. Cada una está
en `src/views/`, se carga bajo demanda y se dibuja dentro de un solo marco,
`AuthShell` -- la tarjeta en `classic`, el hero y el panel de vidrio en
`nexora` --, así que las páginas se comparten entre los dos looks y solo cambia
el marco.

| Ruta | Página | Llama a |
| --- | --- | --- |
| `/login` | `LoginView` | `POST /auth/login`, y `/auth/login/mfa` cuando falta el código |
| `/register` | `RegisterView` | `POST /auth/register` |
| `/verify-email?token=` | `VerifyEmailView` | `POST /auth/verify` |
| `/forgot-password` | `ForgotPasswordView` | `POST /auth/password/forgot` |
| `/reset-password?token=` | `ResetPasswordView` | `POST /auth/password/reset` |
| `/mfa/enrol` | `MfaEnrolView` | `/auth/mfa/setup` y `/confirm` con el token MFA del sign-in |
| `/account/security` | `SecurityView` | activar y quitar MFA, códigos de recuperación nuevos, cerrar todas las sesiones |

**Iniciar sesión trae al usuario.** `accounts` contesta `/auth/login` solo con
tokens, así que el store de auth (Vue) o `services/auth.service.js` (React) los
guarda y luego llama a `GET /auth/account`; `user` tiene lo que eso devuelve
(`email`, `display_name`, `roles`, `permissions`, `email_verified`,
`mfa_enabled`). Un sign-in también puede quedarse antes de la sesión: `login()`
resuelve a `{ status: 'signed-in' }`, `{ status: 'mfa', mfaToken }` -- la página
pide el código -- o `{ status: 'enrol', mfaToken }`, cuando un rol de la cuenta
exige MFA y no lo tiene, y la página manda al usuario a configurarlo.

**Los links que ofrece una página siguen al backend.** "¿Olvidaste tu
contraseña?", "Crea una" y la tarjeta de MFA solo aparecen cuando
`GET /auth/features` dice que la función está activa, así que un link nunca
lleva a un 404.

**Los tokens de los correos salen de la barra de direcciones.** Las páginas de
verificación y de reset leen `?token=`, reemplazan la URL sin él y lo postean una
vez -- no queda en el historial ni se manda como `Referer`. El token MFA de un
sign-in viaja en el state del router, nunca en la URL. Las dos rutas de los
correos tienen que coincidir con `[plugin.accounts] verify_email_path` y
`reset_password_path`.

**Una contraseña equivocada no es una sesión vencida.** Las llamadas de cuenta
llevan `skipAuthRefresh`, y el interceptor de 401 las deja en paz: sin eso, una
contraseña mal escrita dispararía un refresh y sacaría a la persona del
formulario que está llenando. Los errores traen el `code` del backend
(`email_not_verified`, `mfa_code_invalid`, `mfa_token_invalid`, `token_invalid`)
junto al mensaje, y las páginas deciden por el código, nunca por el texto.

No se dibuja un QR para MFA: sería una dependencia por una imagen. El link
`otpauth://` abre el autenticador en un teléfono, y la clave se muestra para
teclearla en cualquier otro lado. Agrega una librería de QR a `MfaSetupPanel` si
tus usuarios lo esperan.

`?next` solo se sigue cuando es un path, nunca una URL absoluta -- viene de la
barra de direcciones.

**Para el instalador: `frontend_accounts`.** La variable de template que decide
todo lo anterior. Si falta, vale `true`. Pásala en `false` cuando ningún
backend del workspace active `accounts`.

---

## Claro y oscuro

Tres estados, no dos:

| `<html>` | Resultado |
| --- | --- |
| sin atributo | sigue al sistema operativo |
| `data-theme="dark"` | oscuro, diga lo que diga el sistema |
| `data-theme="light"` | claro, diga lo que diga el sistema |

El `dark:` de fábrica de Tailwind solo lee el sistema, lo que deja a quien
quiere el otro sin forma de decirlo. `src/style.css` redefine la variante para
revisar primero una elección explícita:

```css
@custom-variant dark {
  &:where([data-theme="dark"], [data-theme="dark"] *) { @slot; }
  @media (prefers-color-scheme: dark) {
    &:where(:root:not([data-theme="light"]), :root:not([data-theme="light"]) *) { @slot; }
  }
}
```

El `:not([data-theme="light"])` de la segunda mitad es la parte que vale
entender: sin eso, una elección explícita de claro pierde contra un sistema en
oscuro y el switch parece funcionar en una sola dirección.

**El toggle** es `ThemeToggle`, en el header de `LayoutAuthenticated`. Invierte
lo que está en pantalla y lo fija. Al lado, y solo una vez que la elección está
fijada, hay un segundo botón — `followSystem()`, con la etiqueta "Follow the
system theme". Ese no es decoración: el toggle solo puede fijar `light` o
`dark`, así que sin él el primer clic se lleva el tercer estado para siempre y
la app vuelve a ser el switch de dos estados que este diseño existe para
evitar. La elección se guarda bajo `<service>:theme`.

**`resolved` es compartido, no por llamador.** Las dos implementaciones de
`useTheme()` mantienen `theme` *y* `resolved` en scope de módulo — un `computed`
en Vue, un valor que React recalcula en cada render detrás de un set de
suscriptores. Armar `resolved` dentro del composable es la versión que parece
correcta y no lo es: un clic actualiza al componente que lo manejó y a nadie
más, así que un toggle en el header y un switch en ajustes terminan mostrando
íconos opuestos mientras `<html>` lleva uno solo de los dos. Un test para esto
necesita dos componentes, no uno — un único toggle verificando que `data-theme`
cambia está verificando la mitad que nunca estuvo rota.

**El flash está resuelto.** `index.html` lleva doce líneas inline que leen la
elección guardada y ponen el atributo antes del primer pintado. Aplicar el tema
después del mount significa un frame del color equivocado en cada recarga, que
es el defecto más notorio que un switch de tema puede tener.

### Las superficies son tokens, no colores

```css
@theme inline {
  --color-surface: var(--ui-surface);   /* the page */
  --color-panel: var(--ui-panel);       /* cards, sidebar, header */
  --color-elevated: var(--ui-elevated); /* hover, skeletons, code */
  --color-line: var(--ui-line);         /* every border */
  --color-ink: var(--ui-ink);           /* primary text */
  --color-ink-soft: var(--ui-ink-soft); /* secondary */
  --color-ink-faint: var(--ui-ink-faint);
}
```

`inline` es lo que lo hace funcionar: las utilidades generadas emiten
`var(--ui-surface)` en vez de resolver en build time, así que redefinir siete
variables bajo `[data-theme="dark"]` da vuelta toda la interfaz.

Lo que eso compra se ve en los componentes: **ninguno lleva una clase `dark:`**.
`bg-panel` es correcto en los dos temas. Un componente que necesita un valor
claro y uno oscuro en cada línea no tiene un design system, tiene dos temas
hardcodeados que se desincronizan la primera vez que alguien edita uno.

La única excepción es el color de estado: `success`, `warning`, `danger`, `info`
en `BaseBadge` y `ToastHost`. Ahí el tono **es** el significado, así que no
puede salir de un token de superficie compartido. Esos se arman como un tinte al
10% de un solo tono más un `dark:` en el texto nada más, porque un tinte se lee
bien sobre cualquiera de las dos superficies y un `bg-emerald-50` sólido no.

`color-scheme` se define junto a los tokens, así que las barras de scroll, los
date pickers y el cursor de texto — que dibuja el navegador y nunca ve una
clase — combinan con el resto.

---

## Convenciones que los templates imponen

**Una sola instancia de axios.** `src/services/api.js` tiene la base URL, el
timeout (`VITE_API_TIMEOUT`, 60 000 ms por defecto -- un endpoint que llama a un
modelo de lenguaje tarda veinte segundos sin problema, y un timeout más corto
convierte una respuesta lenta en un error que el usuario reintenta, pagando la
llamada dos veces), el manejo de 401 de arriba, y un interceptor que convierte el `detail`
RFC 7807 del backend en `error.message`. Sin eso, cada componente muestra
"Request failed with status code 409" en vez de "Invoice INV-1 already exists".
La única excepción es `plain`, exportado desde el mismo archivo: misma
configuración, sin interceptores, y `/auth/refresh` es todo para lo que sirve —
un refresh que pasara por `api` tendría su propio 401 respondido con otro
refresh.

**HTTP en `Services/`, nunca en componentes.** La página generada llama a
`listFacturas()`; ella maneja el estado de carga y de error, el service maneja
el request.

**Tailwind v4 con tokens.** Un `@import "tailwindcss"` y los bloques `@theme`
de `src/style.css`. Usa `bg-panel`, `border-line` y `text-ink` en vez de una
rampa neutra: un componente que nombra `zinc-200` directo es un componente al
que el tema no llega, y dos componentes que eligen rampas distintas son la
razón por la que una UI termina viéndose sutilmente mal sin que nada esté
identificablemente roto. Ver
[.jfast/skills/design-system/SKILL.md](../.jfast/skills/design-system/SKILL.md).
Nexora conserva las mismas utilidades — `src/style.css` apunta `bg-panel`,
`text-ink` y la rampa `brand-*` a sus propios tokens — y agrega encima sus
clases de componentes, así que una página escrita para cualquiera de los dos
looks toma sus colores de tokens.

**Íconos sin runtime.** `@mdi/js` trae solo strings de path; `BaseIcon`
renderiza uno en un SVG. Sin icon font, tree-shaken a lo que importas.

Específico de Vue: `<script setup>` y la Composition API, nunca Options API;
componentes `PascalCase.vue`; las páginas a nivel de ruta terminan en
`View.vue`.

Específico de React: componentes función y hooks; componentes `PascalCase.jsx`;
las páginas a nivel de ruta terminan en `View.jsx`.

---

## Verificado cómo

Sé preciso con esto, porque importa para cuánto deberías confiar en ello.

**Probado en CI:** cada archivo renderiza; la estructura del módulo cae donde
debe; el router y el sidebar se parchan correctamente y de forma idempotente;
las `{{ }}` de Vue y las llaves de JSX sobreviven al scaffolding; el framework
se detecta desde el proyecto. Y, desde que aterrizaron los componentes base,
**`npm install` seguido de `npm run build` en ambos frontends generados** —
`scripts/smoke_components.sh`, que además verifica que `ToastHost` esté montado
en algún lado, que haya exactamente un store de toasts, que el bundle contenga
una vuelta a `system` y un refresh-on-401, y que el drawer bloquee el scroll del
body. Renderizar un template prueba que las llaves estaban bien; no prueba que
un componente importe algo que existe.

**Ejercitado contra un backend simulado, en Node, a mano:** tres llamadores de
`useTheme()` y un clic, verificando que después los tres coinciden; y cuatro 401
concurrentes contra el `api.js` y el `auth.store.js` reales con un adapter de
axios como servidor, verificando un refresh y cuatro 200, y después lo mismo con
un refresh que a su vez es rechazado. Los dos se escribieron primero y los dos
fallaron con los templates anteriores — el primero con
`["Switch to dark theme", "Switch to light theme", "Switch to light theme"]`, el
segundo con cuatro reintentos llevando el token que acababa de vencer. Son
harnesses descartables, no una suite: **no** están en CI.

**Ejercitado en un navegador real, una vez, a mano:** el switch de tema en los
dos frontends generados — claro, oscuro, una elección explícita de claro contra
un sistema en oscuro, la preferencia sobreviviendo una recarga, y el drawer
móvil abriendo. Los estilos computados se leyeron de vuelta, no se miraron a
ojo. Es una corrida en un navegador, no una suite: no está en CI y no va a
atrapar una regresión.

**Los dos looks, los dos frameworks, en CI:** `scripts/smoke_frontend.sh`
genera Vue y React en `nexora` y en `classic`, corre `jfast new view` en cada
uno y los construye; también comprueba que three.js quede fuera del chunk de
entrada y que el build respete `VITE_ACCENT`. Las pantallas nexora, el listón,
el selector de acento y los dos temas se revisaron a mano en un navegador
cuando se escribieron — una vez, no en CI.

**Las páginas de cuenta:** CI renderiza los dos frameworks en los dos looks y
revisa las rutas, el default y cada endpoint que llama el store. `npm install` y
`npm run build` se corrieron a mano en los cuatro cuando se escribieron. Los
flujos del backend que llaman están probados por HTTP
(`tests/test_accounts_flows.py`); las páginas en sí **no** se han recorrido en
un navegador contra un backend corriendo.

**No probado:** `npm run dev` como sesión interactiva, las páginas de cuenta en
un navegador, y nada más sobre cómo se ve. Que el build pase significa que compila, no que un modal
atrape el foco correctamente con un lector de pantalla real.

---

## Angular

No se genera, a propósito. Un `angular.json` hecho a mano y una config de
builder que nunca corrió con `ng serve` es peor que no tener scaffold: parece
terminado y falla de una forma difícil de atribuir.

Si quieres Angular hoy: haz `ng new` del proyecto tú mismo, y después mantén la
misma estructura `Modulo<Name>` a mano. El soporte del generador es la fase 3
de PLAN.md, y debería aterrizar solo junto a un job de CI que realmente buildee
la salida.
