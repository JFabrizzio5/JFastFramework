# Cifrado

Algunos secretos que guarda un servicio son datos de sus usuarios, y tiene que
poder leerlos de vuelta: la contraseña que abre la firma electrónica de un
cliente, un token de su banco. Un hash no sirve -- el valor se necesita en claro
para usarlo -- y `SecretStr` solo lo mantiene fuera de los logs; la base sigue
guardando el texto plano. `jfastframework.encryption` es para esos casos.

```bash
pip install "jfastframework[encryption]"
python -c "from jfastframework.encryption import generate_key; print(generate_key())"
export JFAST_ENCRYPTION_KEYS="k1:<esa llave>"
```

## En una columna

```python
from jfastframework.encryption import EncryptedString

class Credential(Base, TimestampMixin, TenantMixin):
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    rfc: Mapped[str]
    password: Mapped[str] = mapped_column(EncryptedString(context="fiel"))
```

El ORM cifra en cada escritura y descifra en cada lectura; el código alrededor ve
strings normales. Lo que se guarda se ve así: `jf1.k1.3q8f...` -- el formato, la
llave que lo generó y el valor sellado.

No se puede buscar por él: dos cifrados del mismo valor son distintos, a
propósito. Busca las filas por otra columna (aquí, `rfc`).

## A mano

```python
from jfastframework.encryption import SecretBox

box = SecretBox.from_env()
token = box.encrypt(password, context=f"fiel:{rfc}")
password = box.decrypt(token, context=f"fiel:{rfc}")
```

`context` se autentica junto con el valor. Un token copiado de la fila de un
cliente a la de otro no se descifra ahí -- con el tipo de columna, el contexto es
la columna; a mano, haz que nombre al dueño, como arriba.

## Qué garantiza, y cómo

| | |
| --- | --- |
| Algoritmo | AES-256-GCM, un nonce nuevo de 96 bits por valor |
| Valor alterado | Falla con `DecryptionError`, nunca se descifra a basura |
| Contexto o llave equivocados | `DecryptionError` |
| Llaves | Del entorno, nunca de la base ni de `jfast.toml` |

## Rotar una llave

`JFAST_ENCRYPTION_KEYS` es una lista, con la principal primero:

```bash
JFAST_ENCRYPTION_KEYS="k2:<llave nueva>,k1:<llave vieja>"
```

La primera llave cifra; todas descifran; cada valor nombra la llave que lo
generó. Así que rotar es:

1. Pon la llave nueva primero y deja la vieja después. Despliega. Las escrituras
   nuevas usan `k2` y los valores viejos se siguen leyendo.
2. Reescribe los valores viejos cuando convenga -- `box.rotate(token,
   context=...)` devuelve el mismo valor bajo la llave principal, y no hace nada
   si ya lo estaba. Basta un job en segundo plano sobre la tabla.
3. Cuando ningún valor nombre `k1`, quítala.

**Perder todas las llaves es perder todos los valores**, que es justo el punto de
cifrarlos. Guarda las llaves donde están los demás secretos del servicio -- un
gestor de secretos con `load_secrets()`, no un archivo en el repositorio.

## Lo que no es

- **No es para contraseñas con las que los usuarios inician sesión.** Esas se
  hashean y nunca se descifran: el plugin `accounts` usa argon2id.
- **No se puede buscar ni ordenar por él.** Cifra el valor; indexa otra cosa.
