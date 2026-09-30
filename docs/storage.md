# Storage

Files live on a **named disk**. Your code writes to `"public"` or `"invoices"`;
whether that disk is a directory, an S3 bucket or a MinIO container is
configuration. The same handler works in development and in production without
an edit.

This is Laravel's disk idea, and it is worth copying: the alternative — a
handler that knows it is writing to `/var/app/uploads` — cannot be deployed
anywhere else without a rewrite.

```bash
jfast new service billing --plugins storage
```

## The two default disks

A new service gets two, and the difference between them is the whole point:

| Disk | Visibility | URL | Used for |
| --- | --- | --- | --- |
| `public` | `public` | permanent | avatars, logos, anything already public |
| `private` | `private` | expires | invoices, contracts, exports, uploads |

```toml
[plugin.storage]
default = "public"
serve_local = true                    # development only

[plugin.storage.disks.public]
driver = "local"
root = "storage/public"
visibility = "public"

[plugin.storage.disks.private]
driver = "local"
root = "storage/private"
visibility = "private"
```

A private disk **refuses** to produce a permanent URL. That is not a
convenience check — a permanent URL to a private disk is exactly how invoice
PDFs end up in a search index.

```python
storage = request.app.state.jfast.require("storage")

await storage.disk("public").put("logos/acme.png", data, content_type="image/png")
storage.disk("public").url("logos/acme.png")          # /storage/public/logos/acme.png

await storage.disk("private").put("invoices/1042.pdf", pdf)
storage.disk("private").url("invoices/1042.pdf")      # StorageError
await storage.disk("private").temporary_url("invoices/1042.pdf", expires_in=300)
```

That is from a route. A `@task` or `@subscribe` handler has no request: it
annotates a parameter `TaskContext` and asks it instead --
`ctx.require("storage")`, the same object
([Queues and events](queues-and-events.md#the-apps-providers-llm-storage-the-outbox)).

## Absolute URLs

`url()` is root-relative by default — `/storage/public/logos/acme.png` — which
is right when the page and the file come from the same origin. When they do
not, that URL resolves against the *client's* host and 404s, and a broken
`<img>` shows nothing: no console error, no failed request where you would
look for one.

`public_base_url` fixes it, on either driver:

```toml
[plugin.storage.disks.public]
driver = "local"
root = "storage/public"
visibility = "public"
public_base_url = "https://api.example.com/storage/public"

[plugin.storage.disks.assets]
driver = "s3"
bucket = "acme-assets"
visibility = "public"
public_base_url = "https://cdn.example.com"    # CloudFront in front of the bucket
```

It applies to `url()` only. `temporary_url()` stays on this app's own path
deliberately: `public_base_url` usually names a cache, and a cache in front of
a signed URL hands the object to the next caller after the signature expired.

A disk key that the driver does not understand is now a **startup error**, not
a silent no-op:

```
storage disk 'public' uses driver 'local', which has no setting 'bucket'.
It belongs to the s3 driver.
```

That check exists because `public_base_url` on a local disk was accepted and
dropped for a release, and nothing anywhere said so.

## Temporary URLs

For a local disk the link carries an expiry and an HMAC signature over **both**
the key and that expiry:

```
/storage/private/invoices/1042.pdf?expires=1793491200&signature=Yk3f...
```

Signing the key alone would make one valid link a key to the entire disk; the
holder could edit the path. Signing the expiry alone would let them edit the
path instead. The signature covers both, and comparison is constant-time.

Set the key or temporary URLs do not work:

```bash
JFAST_STORAGE_SIGNING_KEY=$(openssl rand -hex 32)
```

In production the plugin warns at startup when a private local disk has no key,
rather than letting the first user to click a download link discover it; a key
shorter than 32 bytes refuses to boot there. Outside production it stays quiet
-- most services never sign a URL, and a warning on every start of every one of
them is a warning nobody reads -- and `temporary_url()` itself fails with the
variable to set.

An expired link and a forged one return the same 403 with the same message.
Different answers would tell an attacker whether the key exists.

For an S3 disk, `temporary_url()` is a presigned URL and the signing key is
irrelevant — S3 does the signing with its own credentials.

## S3 and MinIO

```toml
[plugin.storage.disks.uploads]
driver = "s3"
bucket = "acme-uploads"
region = "eu-west-1"
visibility = "private"
```

No credentials in the config: boto3's default chain finds the instance role,
the task role, or `~/.aws/credentials`. A task role is better than any key you
could put here, because there is no long-lived secret to leak.

MinIO is S3 with two extra settings:

```toml
[plugin.storage.disks.uploads]
driver = "s3"
bucket = "uploads"
endpoint_url = "http://localhost:8006"
access_key = "jfast"
secret_key = "${MINIO_PASSWORD}"
force_path_style = true              # MinIO needs it, real S3 does not
```

To get the container in your compose file:

```toml
[plugin.storage]
minio_include_infra = true
```

`jfast deploy compose` then emits MinIO at your service's base port `+6`, like
every other plugin's container.

Every S3 call has a deadline, a retry policy and a circuit breaker, per disk:

```toml
[plugin.storage.disks.uploads]
connect_timeout = 5.0      # seconds to connect
read_timeout = 30.0        # seconds of silence while reading
max_attempts = 3           # botocore "standard" retries, first try included
breaker_failures = 5       # failed calls in a row that stop calling S3...
breaker_cool_down = 15.0   # ...for this long; then one probe
```

While the breaker is open an upload fails at once with a 503 instead of holding
a worker thread through three timeouts. A 404 is an answer and never counts.
Presigning is local and is not behind the breaker. The reasons for each number
are in [Resilience](resilience.md).

## Keys are not paths

Every backend runs the key through the same validator before it reaches a
filesystem or a bucket:

- no `..`, no leading `/`, no backslashes, no null bytes;
- `.` and `..` resolved *before* the check, so `a/../../b` is caught here
  rather than by the filesystem;
- letters, digits, `.`, `_`, `-` and `/` only;
- 1024 characters maximum.

Local disks additionally re-check after resolving, because a symlink inside the
disk root can point outside it and only resolution reveals that.

Path traversal is the most common storage vulnerability there is. Validating in
the backend rather than in the plugin means a backend used directly — from a
worker, from a script — is as safe as one used through a route.

## Validating uploads

`put()` on a bare disk accepts any number of bytes of anything. Size and type
rules are not a per-handler concern — every handler that forgets one is the
hole — so they belong to the disk, in a **pipeline**:

```toml
[plugin.storage.disks.uploads]
driver = "s3"
bucket = "acme-uploads"
pipeline = ["validate"]

[plugin.storage.disks.uploads.validate]
max_bytes = "10MB"
allow = ["image/jpeg", "image/png", "application/pdf"]
```

Both settings are optional. Without `max_bytes` there is no size limit; without
`allow`, any type `validate` can recognise is accepted.

The pipeline runs inside the backend, not in the plugin, for the same reason
key validation does: a disk used directly from a worker or a script obeys the
same rules as one used through a route.

### The type comes from the bytes

`validate` reads the content type out of the file's first bytes. Not from the
extension, not from the `Content-Type` the browser sent — both are chosen by
whoever is uploading, and a `.png` on a shell script is the oldest upload
attack there is. The type it reads then **replaces** whatever the caller
declared, because that value is what gets stored and served back on download.

`guess_content_type()` in `jfastframework.storage` is the extension-based one.
It is only ever correct for objects this service wrote itself; do not reach for
it on the way in.

Recognition is a small table of magic numbers, no dependency. It knows:

| | |
| --- | --- |
| images | `image/png` `image/jpeg` `image/gif` `image/webp` `image/tiff` `image/avif` `image/heic` |
| documents | `application/pdf` `application/zip` `application/gzip` |
| media | `video/mp4` `audio/mpeg` `audio/ogg` |
| fonts | `font/woff` `font/woff2` |

**Anything else is rejected**, including something that is perfectly fine.
Waving an unrecognised file through would make the check decorative.

Two consequences worth knowing before you configure a disk:

- **Formats identified by parsing are recognised only where a disk asks for
  them.** XML, JSON, CSV and plain text have no magic number, and a sniffer that
  calls anything printable `text/plain` says yes to a shell script. So they are
  never guessed: a disk that lists one in `allow` gets it, checked by parsing,
  and no other disk does. See below.
- **Every OOXML and OpenDocument file is a zip.** Allowing `.docx` means
  allowing `application/zip`, and that allows every other zip too. Decide that
  in config review, where someone can see it.

Listing a type `validate` cannot recognise is a startup error, not a rule that
silently never matches.

### XML, JSON, CSV and text

A disk for CFDI invoices holds XML, and `validate` can hold it to that:

```toml
[plugin.storage.disks.cfdi]
driver = "local"
root = "storage/cfdi"
pipeline = ["validate"]

[plugin.storage.disks.cfdi.validate]
max_bytes = "5MB"
allow = ["application/xml"]
```

| In `allow` | The file must |
| --- | --- |
| `application/xml` | Be well-formed XML with **no DOCTYPE and no ENTITY declaration** -- that is where XXE and entity-expansion bombs live, and a data file needs neither -- and not be SVG or HTML, which a browser would render and run |
| `application/json` | Parse to an object or an array |
| `text/csv` | Be UTF-8 text that the `csv` module reads into at least one row |
| `text/plain` | Be UTF-8 with no control characters beyond tab and newlines |

They are tried only when the file matched no magic number, and in that order,
so an XML file on a disk that allows both XML and text is XML. A disk that does
not list them rejects them exactly as before.

## Large files: `put_stream`

`put()` takes bytes, so the whole file is in memory before anything is checked.
For a multi-gigabyte archive, stream it instead:

```python
disk = storage.disk("archives")
stored = await disk.put_stream("2026/04/cfdi.zip", request.stream())
```

Any async iterable of bytes works: a request body, a file read in chunks, a
download from somewhere else. On the way:

- **`max_bytes` is enforced as the bytes arrive.** An oversized upload stops at
  the limit instead of after it has filled the disk.
- **The type is decided from the head** -- the first 64 KB -- by the same
  signatures, and for XML, JSON and text by what the head can show: XML opens
  with a tag and declares no DOCTYPE or ENTITY (a DTD comes before the root
  element, so it would be there), JSON opens an object or an array, text
  decodes as UTF-8. A file that fails is refused before anything reaches S3.
- **Nothing is visible until the last byte is written.** Locally the stream goes
  to a temporary file renamed into place; on S3 it is a multipart upload,
  aborted if anything fails, so the bucket keeps no orphan parts.
- **A disk whose pipeline rewrites uploads refuses streams.** `optimise-image`
  needs the whole file; use `put()` there.

### Optimising images

`optimise-image` re-encodes the image formats it recognises on the way in. It
is optional, off unless a disk names it, and it carries Pillow:

```
pip install "jfastframework[images]"
```

```toml
[plugin.storage.disks.uploads]
driver = "s3"
bucket = "acme-uploads"
pipeline = ["validate", "optimise-image"]

[plugin.storage.disks.uploads.optimise-image]
format = "webp"          # webp | jpeg | png | gif | keep
quality = 82             # 1-100; means nothing to png and gif
max_dimension = 2400     # longest edge in pixels; 0 leaves the size alone
strip_metadata = true    # the default
max_pixels = 50000000    # refuse anything that decodes larger than this
```

**It only touches four types**: `image/jpeg`, `image/png`, `image/gif` and
`image/webp`. Everything else is handed on byte for byte, including things it
would clearly be able to open. That is the point: a signed PDF through an image
codec is an invalid PDF, and re-encoding a raw photograph destroys the
negative. `image/tiff` is excluded for exactly that reason — Canon CR2 and
Nikon NEF raws *are* TIFF containers, with the same magic bytes and the same
sniffed type, and nothing at this layer can tell one from the other. `avif` and
`heic` are excluded too, because Pillow decodes neither without a plugin that
may not be in your image.

It reads the type out of the bytes itself rather than trusting
`upload.content_type`, so it behaves the same whether or not `validate` runs in
front of it.

**What it does about the awkward cases**, each of which is a silent data loss
if you get it wrong:

| case | what happens |
| --- | --- |
| animated GIF or WebP | left untouched — a still frame is not the animation |
| alpha channel, `format = "jpeg"` | encoded as WebP instead — JPEG has no alpha, and compositing onto a background colour nobody chose is a silent edit |
| EXIF, XMP, PNG text | dropped, `strip_metadata = true` being the default: EXIF carries GPS coordinates |
| EXIF orientation | applied to the pixels before the tag is dropped, always — stripping the flag without rotating turns a portrait sideways |
| ICC profile | kept even when stripping. A colour profile is colour, not privacy |
| output larger than the input | the original is kept, unless `max_dimension` resized it |
| more pixels than `max_pixels` | `UploadRejected`. A 40 GB expansion out of a small file is the decompression-bomb CVE, and a hostile file is refused rather than stored |
| Pillow cannot decode it | passed through. This step optimises; deciding what is admissible is `validate`'s job |

**The key changes with the bytes.** A local disk reads an object's content type
back out of its key, so `holiday.jpg` holding WebP would be served as
`image/jpeg`. `put()` returns the key it actually wrote — keep that one, not
the one you passed in:

```python
stored = await disk.put("holiday.jpg", data)
stored.key  # 'holiday.webp'
```

**What it saves**, measured in `tests/test_storage_images.py` at
`format = "webp"`, `quality = 82`, Pillow 12.3:

| input | before | after | saving |
| --- | --- | --- | --- |
| JPEG q90 photograph, 1600×1200 | 588,527 B | 311,426 B | 47% |
| PNG photograph, 1600×1200 | 3,175,929 B | 339,070 B | 89% |
| PNG photograph with alpha, 800×600 | 480,285 B | 39,654 B | 92% |
| PNG flat-colour screenshot, 1600×1200 | 9,384 B | 9,054 B | 4% |

That last row is the honest one. The saving is a property of the *input*, not
of the step: a PNG holding a photograph is being stored losslessly and has
everything to gain, a JPEG already lost most of it, and flat colour was already
the case PNG is good at. Nothing here is free — every one of those rows cost a
full decode and re-encode of the image, which is why it happens in a thread.

**It runs in `asyncio.to_thread`.** Image codecs are CPU, not I/O, which is
exactly why they read as harmless: nothing about `Image.open(...)` looks like a
network call, and a 24-megapixel JPEG is still hundreds of milliseconds of
decode and seconds of re-encode with every other request on the worker waiting.
`jfast contracts check` knows about Pillow — `Image.open`, `Image.new`,
`ImageOps`, `ImageFilter` and the methods of anything they return — so a
synchronous re-encode written into an `async def` is a finding rather than a
mystery in production latency.

### Writing a step

`validate` and `optimise-image` are the two that ship, not the only shape of
one. Virus scanning plugs in the same way:

```python
from dataclasses import replace
from jfastframework.storage import Upload, register_step

class Thumbnail:
    name = "thumbnail"

    def __init__(self, disk: str, config: dict) -> None:
        self.width = int(config.get("width", 512))

    async def process(self, upload: Upload) -> Upload:
        smaller = await asyncio.to_thread(shrink, upload.data, self.width)
        return replace(upload, data=smaller)

register_step("thumbnail", Thumbnail)
```

A step takes an `Upload` and returns one. Returning rather than mutating means
a step that rewrites the bytes and one that only inspects them read the same
way, and a step that raises half way through cannot leave a half-rewritten
upload for the next step.

Steps are `async def` because the pipeline runs inside a request, and anything
CPU-bound in one has to go to `asyncio.to_thread` — the same discipline
`LocalStorage.put` follows, and the one `jfast contracts check` enforces.
Rejecting an upload means raising `UploadRejected`, which names the limit and
the actual value:

```
disk 'uploads': 'holiday.jpg' is 24.3MB (25480051 bytes),
over this disk's max_bytes of 10MB (10485760 bytes)
```

## Serving files

`serve_local = true` mounts `/storage/{disk}/{key}` so downloads work with
nothing else running. It is for development.

In production, put Caddy or a CDN in front of the public disk and set
`serve_local = false`. A Python worker holding a connection open to stream a
40 MB PDF is a worker not serving requests. The plugin logs a warning if it
finds itself serving files in production.

Downloads are sent as `Content-Disposition: attachment` with
`X-Content-Type-Options: nosniff`. An uploaded `.html` or `.svg` rendered
inline runs the uploader's script on your origin, against your users' cookies —
so uploads are saved, not rendered. Pass `inline=True` to
`sanitised_download_headers()` only for files your own code produced.

## Moving a file between disks

`/storage/{disk}/{key}` names the disk, so moving an object from `local` to
`s3` 404s every URL ever handed out. That is usually the thing blocking a
migration to S3, and it is why `/storage/{key}` exists — it names the object
and lets the app work out where it lives:

```toml
[plugin.storage]
resolve_by_key = true
resolve_strategy = "recorded"     # or "probe"
```

Off by default. `/storage/{disk}/{key}` keeps working either way; a first path
segment that matches a disk name is always read as a disk name, which is the
price of not breaking the older shape.

### The two strategies, and what each costs

**`recorded`** — the default. Whoever wrote the object also wrote down which
disk took it, and resolution is one lookup:

```python
storage = request.app.state.jfast.require("storage")
await storage.disk("uploads").put(key, data)
await storage.record(key, "uploads")
```

Exact and cheap. It only knows about objects that were recorded, so it does
nothing for the files already sitting on the old disk the day you turn it on,
and every write now has a second thing that can be forgotten.

The default ledger is **in memory**: fine for tests and a single-process dev
server, wrong everywhere else, because two replicas do not share it and a
restart loses every mapping. Point it at the row you already have:

```python
class Files:
    async def record(self, key: str, disk: str) -> None: ...
    async def locate(self, key: str) -> str | None: ...
    async def forget(self, key: str) -> None: ...

storage.use_ledger(Files())
```

**`probe`** — for a migration window. Try disks in order and take the first
that has the object:

```toml
[plugin.storage]
resolve_by_key = true
resolve_strategy = "probe"
read_order = ["s3", "local"]      # newest first
copy_on_read = true
```

No bookkeeping and it works on day one, including for files written years ago.
It costs a round trip per disk that does *not* hold the object — a miss is a
`HEAD` against every disk in the list before the 404, and against S3 that is
real latency on your 404 path. It is a window, not a steady state.

`copy_on_read` closes the window as traffic flows: when probing finds an object
on an older disk it is copied to the first disk in `read_order` and recorded,
so the next request is a hit. It turns a `GET` into a read plus a write, so the
first request for every object pays for a full copy — fine for a few thousand
avatars, wrong for a bucket of video. It is opt-in for that reason.

### Moving one, explicitly

```python
await storage.copy(key, "local", "s3")     # both disks hold it
await storage.move(key, "local", "s3")     # copy, delete, re-point the ledger
```

`move` copies before it deletes: a crash in between leaves two copies, which is
recoverable. Neither runs the target disk's pipeline — the object is already
stored, and re-validating it would let a rule tightened today reject a file that
was legal when it was written, in the middle of a migration.

### Signed links that survive the move

```python
storage.stable_url(key)                              # /storage/invoices/1042.pdf
await storage.stable_temporary_url(key, expires_in=300)
```

These are signed with the service's key rather than the disk's, so the link
still verifies after the object has moved to a disk that does no signing of its
own. The visibility rule is unchanged: whichever disk the key resolves to
decides whether a signature is required.

## Local disks in the production image

The generated image runs as `appuser` (uid 10001), and the generated compose
file mounts a named volume on every local disk's root
(`<service>_<disk>_data`). Docker creates a volume's mount point as root when
the image does not already have that directory, so each local root has to
exist in the image, owned by `appuser`. `jfast deploy dockerfile` writes one
`mkdir`/`chown` line for every local disk in `[plugin.storage.disks]`, and
`jfast add storage` rewrites that line alone in a Dockerfile you have edited.
After declaring a disk:

```bash
jfast add storage          # the Dockerfile creates the new root for appuser
jfast workspace compose    # its volume (`jfast deploy compose` for a lone service)
docker compose up --build
```

Skip the first and `/ready` answers 503 (`/app/storage/adjuntos is not
writable`) while `/health` stays 200, and the first upload is a 500
`PermissionError`. `jfast upgrade --check` names every root the Dockerfile does
not create (`image-cannot-write-local-storage`).

### A volume created as root

Docker copies the image's directory, owner included, into a named volume that
is empty when it is mounted. So a volume created as root before the image had
the directory is fixed by the rebuilt image only while it is still empty; once
it holds a file it stays root's, `/ready` stays 503, and rebuilding changes
nothing. Hand it over once, as root, with the data in place:

```bash
docker compose run --rm --no-deps --user root <service> \
  chown -R appuser:appuser /app/storage/adjuntos
```

`<service>` is the API's compose service -- `api` from `jfast deploy compose`,
the service's own name in a workspace. The worker mounts the same volume, so
once is enough. An empty volume can be removed instead (`docker volume rm
<project>_<service>_adjuntos_data`); the next `up` creates it from the image.

## Health

`/ready` reports each disk: a local disk that is missing or read-only, an S3
bucket that cannot be reached. The two are not the same failure:

- **A local disk** belongs to this replica. Missing or read-only, it fails
  readiness, so the orchestrator stops sending traffic to the one replica with
  the broken volume.
- **An object store** is shared by every replica. Failing readiness over it
  would take all of them out of rotation and fix nothing, so it is reported
  *degraded*: `/ready` answers 200 and names the bucket.

Each disk answers inside the readiness budget. The S3 probe makes one short
try, and while the disk's breaker is open it is not probed at all.

## What this does not do

- **No thumbnails.** `optimise-image` re-encodes *one* object into one object.
  Deriving a set of sizes from an upload is a job, not a pipeline step, because
  a pipeline step that produces four objects has nowhere to put three of them.
- **No streaming downloads and no range requests.** The download route reads
  the whole object into memory before it answers. A 40 MB PDF is 40 MB of
  resident memory per concurrent download, and a video served through it cannot
  be seeked. Put Caddy or a CDN in front of anything large.
- **No virus scanning.** If you accept uploads from the public, you need it;
  it is not here. It is the other step the pipeline was shaped for.

## See also

- [Multi-tenancy](multitenancy.md) — per-tenant prefixes and subdomains
- [Deployment](deploy.md) — Caddy in front of the public disk
