"""Workspace-level compose and Caddyfile generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from jfastframework.deploy.workspace import (
    build_workspace_compose,
    render_caddyfile,
    render_workspace_compose,
)
from jfastframework.workspace import ServiceEntry, Workspace


def ws(*services: ServiceEntry) -> Workspace:
    workspace = Workspace(name="cometax")
    for service in services:
        workspace.add(service)
    return workspace


def on_disk(root: Path, *services: ServiceEntry) -> Workspace:
    """A workspace whose services have a directory and a ``jfast.toml``.

    The plugin graph lives per service, in that file. A workspace held only in
    memory has nowhere to read it from, which is why every plugin assertion
    needs a real directory.
    """
    workspace = Workspace(name="cometax", file=root / "jfast.workspace.toml")
    for service in services:
        workspace.add(service)
    workspace.save()
    return workspace


def service_config(root: Path, service: ServiceEntry, body: str = "") -> None:
    directory = root / service.path
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "jfast.toml").write_text(body, encoding="utf-8")


def api(name: str, port: int, **kwargs: object) -> ServiceEntry:
    return ServiceEntry(name=name, kind="api", port=port, path=name, **kwargs)  # type: ignore[arg-type]


def spa(name: str, port: int) -> ServiceEntry:
    return ServiceEntry(name=name, kind="spa", port=port, path=name, frontend="vue")


def gateway(port: int) -> ServiceEntry:
    return ServiceEntry(name="gateway", kind="gateway", port=port, path="gateway")


# -- compose ------------------------------------------------------------


def test_every_backend_becomes_a_service() -> None:
    compose = build_workspace_compose(ws(api("billing", 8010), api("catalog", 8020)))
    assert "billing" in compose["services"]
    assert "catalog" in compose["services"]


def test_a_frontend_is_not_a_container() -> None:
    compose = build_workspace_compose(ws(api("billing", 8010), spa("admin", 8020)))
    # A built SPA is static files; Caddy serves them from <frontend>/dist. Running a
    # Node container in production to serve them is a process nobody needs.
    assert "admin" not in compose["services"]


def test_caddy_mounts_the_frontends_own_dist() -> None:
    caddy = build_workspace_compose(ws(api("billing", 8010), spa("admin", 8020)))["services"][
        "caddy"
    ]
    assert "./admin/dist:/srv:ro" in caddy["volumes"]
    assert "./dist:/srv:ro" not in caddy["volumes"]


def test_without_a_frontend_caddy_mounts_no_site() -> None:
    caddy = build_workspace_compose(ws(api("billing", 8010)))["services"]["caddy"]
    assert not any(volume.endswith(":/srv:ro") for volume in caddy["volumes"])
    assert "./Caddyfile:/etc/caddy/Caddyfile:ro" in caddy["volumes"]


def test_the_compose_header_names_the_directory_caddy_serves() -> None:
    rendered = render_workspace_compose(ws(api("billing", 8010), spa("admin", 8020)))
    assert "by Caddy from ./admin/dist" in rendered


def test_declared_datastores_become_containers() -> None:
    compose = build_workspace_compose(ws(api("billing", 8010, datastores=["database", "cache"])))
    assert "billing-database" in compose["services"]
    assert "billing-cache" in compose["services"]


def test_datastores_are_namespaced_per_service() -> None:
    compose = build_workspace_compose(
        ws(
            api("billing", 8010, datastores=["database"]),
            api("catalog", 8020, datastores=["database"]),
        )
    )
    # Two services asking for `database` get two instances. Splitting services
    # to share one database is a split that bought nothing.
    assert "billing-database" in compose["services"]
    assert "catalog-database" in compose["services"]


def test_datastore_ports_follow_the_block_offsets() -> None:
    compose = build_workspace_compose(
        ws(api("billing", 8010, datastores=["database", "cache", "qdrant"]))
    )
    assert compose["services"]["billing-database"]["ports"] == ["8011:5432"]
    assert compose["services"]["billing-cache"]["ports"] == ["8013:6379"]
    assert compose["services"]["billing-qdrant"]["ports"] == ["8017:6333"]


def test_a_service_waits_for_a_healthy_database() -> None:
    compose = build_workspace_compose(ws(api("billing", 8010, datastores=["database"])))
    assert compose["services"]["billing"]["depends_on"]["billing-database"] == {
        "condition": "service_healthy"
    }


def test_grpc_publishes_its_own_port() -> None:
    compose = build_workspace_compose(ws(api("edge", 8010, grpc=True)))
    assert compose["services"]["edge"]["ports"] == ["8010:8010", "8019:8019"]


def test_caddy_can_be_left_out() -> None:
    compose = build_workspace_compose(ws(api("billing", 8010)), with_caddy=False)
    assert "caddy" not in compose["services"]


def test_the_rendered_compose_is_valid_yaml() -> None:
    yaml = pytest.importorskip("yaml")
    workspace = ws(api("billing", 8010, datastores=["database"]), spa("admin", 8020))
    parsed = yaml.safe_load(render_workspace_compose(workspace))

    assert parsed["services"]["billing"]["build"]["context"] == "./billing"
    assert "billing_database_data" in parsed["volumes"]


# -- the plugin graph ---------------------------------------------------


def test_a_plugin_that_declares_infra_becomes_a_container(tmp_path: Path) -> None:
    """`events` owns a broker. Nothing in the resource graph can say so."""
    billing = api("billing", 8010)
    workspace = on_disk(tmp_path, billing)
    service_config(
        tmp_path,
        billing,
        '[app]\nname = "billing"\nport = 8010\n\n[plugins]\nenabled = ["events"]\n',
    )

    compose = build_workspace_compose(workspace, with_caddy=False)

    assert "kafka" in compose["services"], "the broker the plugin declares is missing"
    # Inside billing's ten-port block, like every other offset. Which internal
    # port it maps to is the plugin's business, not this generator's.
    published = compose["services"]["kafka"]["ports"][0].partition(":")[0]
    assert 8010 <= int(published) < 8020
    assert "kafka" in compose["services"]["billing"]["depends_on"]
    assert "kafka_data" in compose["volumes"]
    # The plugin was told which base port it is published on, so the address it
    # advertises to clients outside the compose network is one they can reach.
    environment = compose["services"]["kafka"].get("environment", {})
    assert any(published in value for value in environment.values())


def test_the_resource_graph_still_owns_the_datastores(tmp_path: Path) -> None:
    """`database` declares infra too, and the resource graph already has it.

    Emitting both would put a second, nameless PostgreSQL beside the one the
    workspace file declares -- and only one of them has a DSN pointing at it.
    """
    billing = api("billing", 8010, datastores=["database"])
    workspace = on_disk(tmp_path, billing)
    service_config(
        tmp_path,
        billing,
        '[app]\nname = "billing"\nport = 8010\n\n[plugins]\nenabled = ["database"]\n',
    )

    compose = build_workspace_compose(workspace, with_caddy=False)

    assert "billing-database" in compose["services"]
    assert "postgres" not in compose["services"]


def test_one_broker_when_two_services_publish_to_it(tmp_path: Path) -> None:
    """The broker advertises its own container name, so there can be one."""
    billing = api("billing", 8010)
    catalog = api("catalog", 8020)
    workspace = on_disk(tmp_path, billing, catalog)
    for service in (billing, catalog):
        service_config(
            tmp_path,
            service,
            f'[app]\nname = "{service.name}"\nport = {service.port}\n\n'
            '[plugins]\nenabled = ["events"]\n',
        )

    compose = build_workspace_compose(workspace, with_caddy=False)

    assert len([name for name in compose["services"] if name == "kafka"]) == 1
    assert "kafka" in compose["services"]["billing"]["depends_on"]
    assert "kafka" in compose["services"]["catalog"]["depends_on"]


def test_a_plugin_that_cannot_be_inspected_is_loud(tmp_path: Path) -> None:
    """Silence is the defect. A plugin we cannot read may own a container."""
    billing = api("billing", 8010)
    workspace = on_disk(tmp_path, billing)
    service_config(
        tmp_path,
        billing,
        '[app]\nname = "billing"\nport = 8010\n\n[plugins]\nenabled = ["nosuchplugin"]\n',
    )

    with pytest.warns(UserWarning, match="nosuchplugin"):
        build_workspace_compose(workspace, with_caddy=False)


def test_local_storage_disks_survive_a_rebuild(tmp_path: Path) -> None:
    """Uploads written into the image are gone on the next `docker build`."""
    billing = api("billing", 8010)
    workspace = on_disk(tmp_path, billing)
    service_config(
        tmp_path,
        billing,
        '[app]\nname = "billing"\nport = 8010\n\n[plugins]\nenabled = ["storage"]\n',
    )

    compose = build_workspace_compose(workspace, with_caddy=False)

    mounts = compose["services"]["billing"]["volumes"]
    assert "billing_public_data:/app/storage/public" in mounts
    assert "billing_private_data:/app/storage/private" in mounts
    assert "billing_public_data" in compose["volumes"]


def test_postgres_gets_more_than_64mb_of_shared_memory() -> None:
    """A parallel query needs /dev/shm; 64 MB is where it starts failing."""
    compose = build_workspace_compose(
        ws(api("billing", 8010, datastores=["database"])), with_caddy=False
    )
    assert compose["services"]["billing-database"]["shm_size"]


# -- Caddyfile ----------------------------------------------------------


def test_one_backend_is_served_under_api() -> None:
    caddyfile = render_caddyfile(ws(api("billing", 8010)))
    # /api whether or not there is a gateway, so the frontend's production
    # build keeps working the day one appears.
    assert "handle /api/* {" in caddyfile
    assert "reverse_proxy billing:8010" in caddyfile


def test_a_gateway_takes_over_the_api_prefix() -> None:
    caddyfile = render_caddyfile(ws(api("billing", 8010), api("catalog", 8020), gateway(8030)))
    assert "reverse_proxy gateway:8030" in caddyfile
    assert "reverse_proxy billing:8010" not in caddyfile


def test_several_backends_without_a_gateway_are_namespaced_under_api() -> None:
    caddyfile = render_caddyfile(ws(api("billing", 8010), api("catalog", 8020)))
    assert "handle /api/billing/* {" in caddyfile
    assert "handle /api/catalog/* {" in caddyfile


def test_the_spa_gets_try_files_so_refresh_works() -> None:
    caddyfile = render_caddyfile(ws(api("billing", 8010), spa("admin", 8020)))
    # Without try_files, a hard refresh on /orders is a 404 from the file
    # server rather than the SPA's own route.
    assert "try_files {path} /index.html" in caddyfile


def test_local_development_disables_automatic_https() -> None:
    assert "auto_https off" in render_caddyfile(ws(api("billing", 8010)))


def test_production_leaves_automatic_https_on() -> None:
    caddyfile = render_caddyfile(
        ws(api("billing", 8010)), hostname="app.example.com", local_dev=False
    )
    assert "auto_https off" not in caddyfile
    assert caddyfile.count("app.example.com {") == 1


# -- the Caddyfile Caddy actually accepts --------------------------------

CADDY_SHAPES = {
    "one": lambda: ws(api("billing", 8010), spa("admin", 8020)),
    "gateway": lambda: ws(api("billing", 8010), api("catalog", 8020), gateway(8030)),
    "several": lambda: ws(api("billing", 8010), api("catalog", 8020), spa("admin", 8040)),
}
CADDY_VARIANTS: dict[str, dict[str, object]] = {
    "plain": {},
    "wildcard": {"wildcard_tenants": True},
    "production": {"hostname": "app.example.com", "local_dev": False},
    "production-wildcard": {
        "hostname": "app.example.com",
        "local_dev": False,
        "wildcard_tenants": True,
    },
}


def _every_caddyfile() -> dict[str, str]:
    return {
        f"{shape}-{variant}": render_caddyfile(build(), **options)  # type: ignore[arg-type]
        for shape, build in CADDY_SHAPES.items()
        for variant, options in CADDY_VARIANTS.items()
    }


def _site_level(caddyfile: str) -> list[str]:
    """Directive names at depth one: inside a site block, outside any other."""
    depth = 0
    found: list[str] = []
    for raw in caddyfile.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "}":
            depth -= 1
            continue
        if depth == 1:
            found.append(line.split()[0])
        if line.endswith("{"):
            depth += 1
    return found


@pytest.mark.parametrize("name", sorted(_every_caddyfile()))
def test_no_header_up_at_site_level(name: str) -> None:
    """0.1.0a12 wrote `header_up` at site level for --wildcard-tenants, and
    Caddy refused the file: `unrecognized directive: header_up`. It is not
    written at all now -- reverse_proxy already sends Host and
    X-Forwarded-Host, and Caddy calls the explicit one unnecessary."""
    caddyfile = _every_caddyfile()[name]
    assert "header_up" not in _site_level(caddyfile)
    assert "\theader_up" not in caddyfile
    if "wildcard" in name:
        assert "*.localhost" in caddyfile or "*.app.example.com" in caddyfile


def test_on_demand_tls_carries_only_ask() -> None:
    """Caddy 2.11 refuses on_demand_tls `interval` and `burst`."""
    caddyfile = render_caddyfile(
        ws(api("billing", 8010)),
        hostname="app.example.com",
        local_dev=False,
        wildcard_tenants=True,
    )
    assert "ask http://billing:8010/internal/tenant-exists" in caddyfile
    assert "interval" not in caddyfile
    assert "burst" not in caddyfile


def _docker() -> str | None:
    import shutil

    for candidate in (
        shutil.which("docker"),
        "/usr/local/bin/docker",
        "/Applications/Docker.app/Contents/Resources/bin/docker",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def test_caddy_validates_every_generated_caddyfile(tmp_path: Path) -> None:
    """The real binary, not a reading of the grammar: the image compose runs."""
    import subprocess  # nosec B404 - fixed argv

    docker = _docker()
    if docker is None:
        pytest.skip("docker is not installed")
    if subprocess.run([docker, "info"], capture_output=True, check=False).returncode != 0:  # nosec B603
        pytest.skip("the docker daemon is not running")
    for name, caddyfile in _every_caddyfile().items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "Caddyfile").write_text(caddyfile, encoding="utf-8")
    script = (
        "for f in /configs/*/Caddyfile; do "
        'echo "== $f"; caddy validate --adapter caddyfile --config "$f" || exit 1; '
        "done"
    )
    done = subprocess.run(  # nosec B603
        [
            docker,
            "run",
            "--rm",
            "-v",
            f"{tmp_path}:/configs:ro",
            "caddy:2-alpine",
            "sh",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if done.returncode != 0 and "Unable to find image" in done.stderr and "== " not in done.stdout:
        pytest.skip("caddy:2-alpine could not be pulled")
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
    assert done.stdout.count("Valid configuration") + done.stderr.count(
        "Valid configuration"
    ) == len(_every_caddyfile()), done.stdout + done.stderr
    # Not only accepted: nothing Caddy would complain about on every start --
    # an unformatted file, a redundant header, a deprecated option.
    warnings = [line for line in done.stderr.splitlines() if '"level":"warn"' in line]
    assert warnings == [], "\n".join(warnings)
