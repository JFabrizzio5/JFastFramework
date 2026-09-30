"""Frontend looks: `--template nexora|classic`, and that the choice sticks.

The complaint these tests answer: a user asked for a look other than the
default and did not get it. So the rules under test are that the default is
nexora, that classic is still exactly the tree it always was, that a look
nobody ships is refused rather than ignored, and that the choice is recorded
where `jfast new view` reads it -- a new page drawn in the other look is the
same failure one command later.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jfastframework.cli.main import app
from jfastframework.cli.scaffold import (
    DEFAULT_FRONTEND_TEMPLATE,
    FRONTEND_TEMPLATES,
    STAMP_FILE,
    TEMPLATE_ROOT,
    Scaffolder,
    Tree,
    detect_frontend_template,
    service_trees,
    view_trees,
)

runner = CliRunner()

FRAMEWORKS = ("vue", "react")
PAGE = {"vue": "FacturasView.vue", "react": "FacturasView.jsx"}
ROUTER = {"vue": "src/router/index.js", "react": "src/router/index.jsx"}

#: Something only the page of that look contains.
PAGE_MARK = {"nexora": "erp-table", "classic": "bg-brand-600"}
#: The runtime braces the page must still carry after scaffolding.
BRACES = {"vue": "{{ item.name }}", "react": "{item.name}"}


def _generate(tmp_path: Path, framework: str, *extra: str, name: str = "web") -> Path:
    result = runner.invoke(
        app,
        ["new", "service", name, "--kind", "spa", "--frontend", framework, *extra],
    )
    assert result.exit_code == 0, result.output
    return tmp_path / name


def _stamped_look(root: Path) -> set[str]:
    templates = json.loads((root / STAMP_FILE).read_text(encoding="utf-8"))["templates"]
    return {
        entry["context"]["frontend_template"]
        for name, entry in templates.items()
        if name.startswith("frontend_")
    }


# ---------------------------------------------------------------------------
# The default
# ---------------------------------------------------------------------------


def test_nexora_is_the_default_look() -> None:
    assert DEFAULT_FRONTEND_TEMPLATE == "nexora"
    assert FRONTEND_TEMPLATES == ("nexora", "classic")


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_a_frontend_generated_without_a_template_is_nexora(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str
) -> None:
    monkeypatch.chdir(tmp_path)
    root = _generate(tmp_path, framework)

    assert (root / "src/nexora/nexora.css").is_file()
    picker = "AccentPicker.vue" if framework == "vue" else "AccentPicker.jsx"
    assert (root / "src/components" / picker).is_file()
    assert '"three"' in (root / "package.json").read_text(encoding="utf-8")
    assert _stamped_look(root) == {"nexora"}


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_the_look_is_composed_in_front_of_the_shared_tree(tmp_path: Path, framework: str) -> None:
    """Look first, then what Vue and React share, then the base. The order is
    the mechanism: the first tree to claim a path owns it."""
    trees = [tree.template for tree in service_trees("spa", framework, tmp_path)]
    assert trees == [f"frontend_{framework}_nexora", "frontend_nexora", f"frontend_{framework}"]

    classic = service_trees("spa", framework, tmp_path, frontend_template="classic")
    assert [tree.template for tree in classic] == [f"frontend_{framework}"]


# ---------------------------------------------------------------------------
# Classic is the tree it always was
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_classic_writes_exactly_the_base_tree_and_nothing_of_nexora(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str
) -> None:
    monkeypatch.chdir(tmp_path)
    root = _generate(tmp_path, framework, "--template", "classic")

    expected = {
        path.relative_to(TEMPLATE_ROOT / f"frontend_{framework}").as_posix().removesuffix(".j2")
        for path in (TEMPLATE_ROOT / f"frontend_{framework}").rglob("*.j2")
    }
    written = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name not in (STAMP_FILE, ".dockerignore")
    }
    assert written == expected

    # The base tree carries a few `frontend_template` branches (package.json,
    # README, .env, vite.config.js). None of them may leak into this look.
    words = ("nexora", "Nexora", "VITE_ACCENT")
    leaked = [
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
        and path.name != STAMP_FILE
        and any(word in path.read_text(encoding="utf-8") for word in words)
    ]
    assert leaked == []
    assert '"three"' not in (root / "package.json").read_text(encoding="utf-8")
    assert _stamped_look(root) == {"classic"}


# ---------------------------------------------------------------------------
# A look nobody ships is refused, not ignored
# ---------------------------------------------------------------------------


def test_an_unknown_template_is_refused_naming_the_real_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["new", "service", "web", "--kind", "spa", "--frontend", "vue", "--template", "glass"]
    )

    assert result.exit_code != 0
    assert "nexora" in result.output and "classic" in result.output
    assert not (tmp_path / "web").exists(), "a refused look must not leave a half-written tree"


def test_a_look_on_a_service_that_draws_nothing_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accepted and ignored would be worse than refused: whoever typed it
    believes it did something."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["new", "service", "billing", "--template", "classic"])

    assert result.exit_code != 0
    assert "spa" in result.output
    assert not (tmp_path / "billing").exists()


def test_start_refuses_an_unknown_template_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["start", "shop", "--template", "glass"])

    assert result.exit_code != 0
    assert "classic" in result.output
    assert list(tmp_path.iterdir()) == []


def test_the_scaffold_functions_refuse_it_too() -> None:
    with pytest.raises(ValueError, match="nexora, classic"):
        service_trees("spa", "vue", Path("x"), frontend_template="glass")
    with pytest.raises(ValueError, match="nexora, classic"):
        view_trees("vue", Path("x"), frontend_template="glass")


def test_new_view_refuses_an_unknown_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    root = _generate(tmp_path, "vue")
    result = runner.invoke(app, ["new", "view", "Facturas", "--root", str(root), "-T", "glass"])

    assert result.exit_code != 0
    assert not (root / "src/ModuloFacturas").exists()


# ---------------------------------------------------------------------------
# The choice is recorded, and `jfast new view` follows it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template", FRONTEND_TEMPLATES)
@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_new_view_draws_the_page_in_the_projects_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str, template: str
) -> None:
    monkeypatch.chdir(tmp_path)
    root = _generate(tmp_path, framework, "--template", template)
    assert detect_frontend_template(root) == template

    result = runner.invoke(app, ["new", "view", "Facturas", "--root", str(root)])
    assert result.exit_code == 0, result.output

    page = (root / "src/ModuloFacturas/Pages" / PAGE[framework]).read_text(encoding="utf-8")
    other = next(look for look in FRONTEND_TEMPLATES if look != template)
    assert PAGE_MARK[template] in page
    assert PAGE_MARK[other] not in page
    assert BRACES[framework] in page, "the runtime braces were eaten at scaffold time"
    # Only the page differs by look; the route and the service are shared.
    assert "ModuloFacturas" in (root / ROUTER[framework]).read_text(encoding="utf-8")
    assert (root / "src/ModuloFacturas/Services/facturas.service.js").is_file()


def test_template_on_new_view_overrides_the_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    root = _generate(tmp_path, "react")
    result = runner.invoke(
        app, ["new", "view", "Facturas", "--root", str(root), "--template", "classic"]
    )

    assert result.exit_code == 0, result.output
    page = (root / "src/ModuloFacturas/Pages/FacturasView.jsx").read_text(encoding="utf-8")
    assert PAGE_MARK["classic"] in page


def test_a_project_without_a_stamp_gets_the_classic_page_and_is_told(tmp_path: Path) -> None:
    """Classic was the only look before looks existed, and its page needs
    nothing but Tailwind. A nexora page would name classes that are not there."""
    root = tmp_path / "web"
    (root / "src/router").mkdir(parents=True)
    (root / "src/router/index.js").write_text("const routes = [\n  /*nuevaRuta*/\n]\n")
    (root / "src/menuAside.js").write_text("export default [\n  /*nuevoModulo*/\n]\n")

    result = runner.invoke(app, ["new", "view", "Facturas", "--root", str(root)])

    assert result.exit_code == 0, result.output
    assert "classic" in result.output
    page = (root / "src/ModuloFacturas/Pages/FacturasView.vue").read_text(encoding="utf-8")
    assert PAGE_MARK["classic"] in page


def test_a_stamp_from_before_looks_existed_reads_as_classic(tmp_path: Path) -> None:
    (tmp_path / STAMP_FILE).write_text(
        json.dumps({"templates": {"frontend_vue": {"context": {"frontend": "vue"}}}})
    )
    assert detect_frontend_template(tmp_path) == "classic"


@pytest.mark.parametrize("body", ["", "not json", "[]", '{"templates": []}'])
def test_an_unreadable_stamp_is_no_answer_rather_than_a_crash(tmp_path: Path, body: str) -> None:
    (tmp_path / STAMP_FILE).write_text(body)
    assert detect_frontend_template(tmp_path) is None


def test_a_backend_stamp_says_nothing_about_a_look(tmp_path: Path) -> None:
    (tmp_path / STAMP_FILE).write_text(json.dumps({"templates": {"service_base": {"context": {}}}}))
    assert detect_frontend_template(tmp_path) is None


# ---------------------------------------------------------------------------
# The interactive installer asks, and does what it was told
# ---------------------------------------------------------------------------


def test_init_asks_for_the_look_and_uses_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    # kind, framework, look, agent surface, workspace, base port.
    answers = "spa\nreact\nclassic\nn\nn\n\n"
    result = runner.invoke(app, ["init", "web"], input=answers)

    assert result.exit_code == 0, result.output
    assert "Which look?" in result.output
    assert _stamped_look(tmp_path / "web") == {"classic"}
    assert not (tmp_path / "web/src/nexora").exists()


# ---------------------------------------------------------------------------
# Composition: the first tree to claim a path owns it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("force", [False, True])
def test_an_overlay_wins_over_the_tree_beneath_it_even_under_force(
    tmp_path: Path, force: bool
) -> None:
    """`--force` re-renders everything. If the last write won, it would hand
    every file a look overrides back to the shared tree underneath."""
    templates = tmp_path / "templates"
    for name, body in (("look", "look\n"), ("base", "base\n")):
        (templates / name).mkdir(parents=True)
        (templates / name / "shared.txt.j2").write_text(body)
    (templates / "base" / "only_base.txt.j2").write_text("base\n")

    target = tmp_path / "out"
    written = Scaffolder(templates).render_trees(
        [Tree("look", target), Tree("base", target)], {}, force=force
    )

    assert (target / "shared.txt").read_text() == "look\n"
    assert (target / "only_base.txt").read_text() == "base\n"
    assert sorted(file.path.name for file in written) == ["only_base.txt", "shared.txt"]


# ---------------------------------------------------------------------------
# Background, the folding sidebar, and the suite as a reference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_the_viewer_can_pick_3d_2d_or_no_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str
) -> None:
    monkeypatch.chdir(tmp_path)
    root = _generate(tmp_path, framework, name="shop_web")

    backdrop = (root / "src/nexora/backdrop.js").read_text(encoding="utf-8")
    assert "'shop-web:backdrop'" in backdrop
    for value in ("'liquid'", "'still'", "'none'"):
        assert value in backdrop
    # The ribbon's loader asks before downloading three.js.
    assert "currentBackdrop() !== 'liquid'" in (root / "src/nexora/background.js").read_text(
        encoding="utf-8"
    )
    # Applied before the first paint, with the project's default from .env.
    index = (root / "index.html").read_text(encoding="utf-8")
    assert "%VITE_BACKGROUND%" in index and "shop-web:backdrop" in index
    assert "VITE_BACKGROUND=" in (root / ".env").read_text(encoding="utf-8")
    picker = "AccentPicker.vue" if framework == "vue" else "AccentPicker.jsx"
    assert "BACKDROPS" in (root / "src/components" / picker).read_text(encoding="utf-8")


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_the_menu_button_folds_the_sidebar_on_a_wide_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str
) -> None:
    monkeypatch.chdir(tmp_path)
    root = _generate(tmp_path, framework)

    layout = "LayoutAuthenticated.vue" if framework == "vue" else "LayoutAuthenticated.jsx"
    source = (root / "src/layouts" / layout).read_text(encoding="utf-8")
    assert "setCollapsed" in source and "toggleMenu" in source
    css = (root / "src/nexora/nexora.css").read_text(encoding="utf-8")
    assert 'html[data-sidebar="collapsed"] .nx-main { margin-left: 0; }' in css
    # The rule that hid the button on desktop -- and lost to .nx-round-btn,
    # leaving a button that did nothing -- is gone.
    assert ".nx-menu-toggle { display: none; }" not in css


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_agent_docs_bring_the_suite_as_a_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, framework: str
) -> None:
    monkeypatch.chdir(tmp_path)
    root = _generate(tmp_path, framework, "--agent-docs")

    skill = root / ".jfast/skills/nexora-reference"
    assert "name: nexora-reference" in (skill / "SKILL.md").read_text(encoding="utf-8")
    for page in ("index", "crm", "pagos", "social", "tablas", "widgets"):
        html = (skill / f"suite/{page}.html").read_text(encoding="utf-8")
        assert "assets/css/nexora.css" in html
    # Every image a page points at is there, and arrived intact.
    pages = "".join(p.read_text(encoding="utf-8") for p in (skill / "suite").glob("*.html"))

    for image in set(re.findall(r"assets/img/[\w-]+\.webp", pages)):
        data = (skill / "suite" / image).read_bytes()
        assert data[:4] == b"RIFF" and data[8:12] == b"WEBP", image
    assert "nexora-reference" in (root / ".jfast/skills/design-system/SKILL.md").read_text(
        encoding="utf-8"
    )


def test_the_suite_is_not_copied_without_agent_docs_or_into_classic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    plain = _generate(tmp_path, "vue", name="plain")
    classic = _generate(tmp_path, "vue", "--template", "classic", "--agent-docs", name="classic")
    assert not (plain / ".jfast/skills/nexora-reference").exists()
    assert not (classic / ".jfast/skills/nexora-reference").exists()


def test_a_file_that_is_not_a_template_is_copied_byte_for_byte(tmp_path: Path) -> None:
    root = tmp_path / "templates"
    (root / "t").mkdir(parents=True)
    blob = bytes(range(256)) * 4  # not valid UTF-8, and full of {{ lookalikes
    (root / "t" / "image.bin").write_bytes(blob)
    (root / "t" / "note.txt.j2").write_text("{{ name }}", encoding="utf-8")

    Scaffolder(root).render_tree("t", tmp_path / "out", {"name": "rendered"})
    assert (tmp_path / "out" / "image.bin").read_bytes() == blob
    assert (tmp_path / "out" / "note.txt").read_text(encoding="utf-8") == "rendered"


# ---------------------------------------------------------------------------
# The frontend speaks `accounts`
# ---------------------------------------------------------------------------

LOOKS = ("nexora", "classic")
EXT = {"vue": "vue", "react": "jsx"}
ACCOUNT_PAGES = (
    "RegisterView",
    "VerifyEmailView",
    "ForgotPasswordView",
    "ResetPasswordView",
    "MfaEnrolView",
    "SecurityView",
)


def _render(tmp_path: Path, framework: str, look: str, **extra: object) -> Path:
    from jfastframework.cli.scaffold import service_context

    target = tmp_path / f"{framework}-{look}"
    context = service_context("web", kind="spa", frontend=framework, frontend_template=look)
    context.update(extra)
    Scaffolder().render_trees(
        service_trees("spa", framework, target, frontend_template=look), context
    )
    return target


def _auth_source(root: Path, framework: str) -> str:
    name = "stores/auth.store.js" if framework == "vue" else "services/auth.service.js"
    return (root / "src" / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("look", LOOKS)
@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_a_frontend_is_private_by_default_and_has_every_account_page(
    tmp_path: Path, framework: str, look: str
) -> None:
    root = _render(tmp_path, framework, look)
    router = (root / ROUTER[framework]).read_text(encoding="utf-8")
    assert "const PUBLIC_BY_DEFAULT = false" in router
    for page in ACCOUNT_PAGES:
        assert (root / f"src/views/{page}.{EXT[framework]}").is_file(), page
        assert f"@/views/{page}.{EXT[framework]}" in router, page
    # One frame per look, and the sign-in page is shared.
    shell = (root / f"src/components/AuthShell.{EXT[framework]}").read_text(encoding="utf-8")
    assert ("nx-auth" in shell) is (look == "nexora")
    assert "AuthShell" in (root / f"src/views/LoginView.{EXT[framework]}").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_signing_in_fetches_the_account_and_handles_the_second_step(
    tmp_path: Path, framework: str
) -> None:
    source = _auth_source(_render(tmp_path, framework, "classic"), framework)
    # accounts answers /auth/login with tokens only: the user comes from here.
    assert "api.get('/auth/account')" in source
    for path in (
        "/auth/login/mfa",
        "/auth/register",
        "/auth/verify",
        "/auth/password/forgot",
        "/auth/password/reset",
        "/auth/mfa/setup",
        "/auth/mfa/confirm",
        "/auth/logout/all",
        "/auth/features",
    ):
        assert f"'{path}'" in source, path
    # A wrong password is an answer, not an expired session.
    assert "skipAuthRefresh: true" in source
    api = (tmp_path / f"{framework}-classic/src/services/api.js").read_text(encoding="utf-8")
    assert "!original.skipAuthRefresh" in api


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_the_request_timeout_fits_a_model_call_and_is_configurable(
    tmp_path: Path, framework: str
) -> None:
    root = _render(tmp_path, framework, "classic")
    api = (root / "src/services/api.js").read_text(encoding="utf-8")
    assert "Number(import.meta.env.VITE_API_TIMEOUT) || 60000" in api
    for env in (".env", ".env.example", ".env.production"):
        assert "VITE_API_TIMEOUT=60000" in (root / env).read_text(encoding="utf-8"), env


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_without_accounts_the_frontend_stays_public_and_offers_no_account_pages(
    tmp_path: Path, framework: str
) -> None:
    root = _render(tmp_path, framework, "nexora", frontend_accounts=False)
    router = (root / ROUTER[framework]).read_text(encoding="utf-8")
    assert "const PUBLIC_BY_DEFAULT = true" in router
    assert "RegisterView" not in router and "SecurityView" not in router
    assert "/account/security" not in (root / "src/menuAside.js").read_text(encoding="utf-8")
