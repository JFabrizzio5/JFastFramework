"""The generated Go service speaks the workspace's contract -- checked from Python.

Two kinds of test here. The first only renders the template and needs nothing.
The second is the cross-language proof: tokens minted by the Python auth
plugin's own ``TokenIssuer`` are handed to the generated Go service's
middleware chain, compiled and run by a real Go toolchain, and the tenant it
resolves is compared with the one a JFast app resolves for the same token. It
also checks that a ``traceparent`` sent to the Go service reaches its outgoing
call unchanged.

The Go side runs with ``go`` when it is on PATH (the CI ``go`` job), else in the
``golang:1.23`` image when Docker has it locally. Neither: skipped. The image
is never pulled from here -- that is a download a test run should not start.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, Depends

from jfastframework.auth import MemoryTokenStore, issue
from jfastframework.auth.tokens import TokenClaims
from jfastframework.cli.generate import generate_service
from jfastframework.plugins.builtin.auth import TokenIssuer
from jfastframework.plugins.builtin.tenancy import current_tenant
from jfastframework.testing import build_test_app, client_for

GO_IMAGE = os.environ.get("JFAST_TEST_GO_IMAGE", "golang:1.23")
SECRET = "a-cross-language-secret-at-least-32-bytes-long"
ISSUER = "https://id.example.test/"
AUDIENCE = "edge"
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRACESTATE = "jfast=cross,vendor=abc"


def _docker() -> str | None:
    for candidate in (
        shutil.which("docker"),
        "/usr/local/bin/docker",
        "/Applications/Docker.app/Contents/Resources/bin/docker",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _go_runner() -> list[str] | None:
    """How to run `go` here, or None when there is no way to."""
    if shutil.which("go"):
        return []
    docker = _docker()
    if docker is None:
        return None
    try:
        probe = subprocess.run(
            [docker, "image", "inspect", GO_IMAGE],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return [docker] if probe.returncode == 0 else None


GO = _go_runner()
needs_go = pytest.mark.skipif(
    GO is None,
    reason=f"no go on PATH and no local {GO_IMAGE} image (docker pull {GO_IMAGE})",
)


def _generate(target: Path) -> Path:
    generate_service(
        "edge",
        kind="api",
        port=9310,
        plugins=[],
        frontend=None,
        target=target,
        workspace=None,
        language="go",
    )
    return target


def test_the_go_template_carries_the_whole_contract(tmp_path: Path) -> None:
    root = _generate(tmp_path / "edge")
    jfast = root / "internal" / "jfast"
    for name in ("trace.go", "auth.go", "jwt.go", "tenancy.go", "middleware.go"):
        assert (jfast / name).is_file(), name

    main = (root / "main.go").read_text(encoding="utf-8")
    for middleware in ("jfast.Trace", "jfast.Authenticate(auth)", "jfast.ResolveTenant(tenancy)"):
        assert middleware in main, middleware

    go_mod = (root / "go.mod").read_text(encoding="utf-8")
    assert "require" not in go_mod, "the Go scaffold must stay stdlib-only"

    for path in root.rglob("*.go"):
        text = path.read_text(encoding="utf-8")
        assert "{{" not in text and "{%" not in text, f"unrendered Jinja in {path.name}"

    env = (root / ".env.example").read_text(encoding="utf-8")
    # The Python plugins' names, not new ones.
    for name in ("JFAST_AUTH_SECRET", "JFAST_AUTH_ALGORITHMS", "JFAST_TENANCY_SOURCES"):
        assert name in env, name


# -- the cross-language proof ----------------------------------------------

# Dropped into the generated service's internal/jfast package by the test: it
# runs the same middleware chain main.go builds, configured only from the
# JFAST_* environment, and writes what it saw for Python to compare.
HARNESS = r"""package jfast

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
)

func TestCrossLanguage(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	authConfig, err := LoadAuthConfig()
	if err != nil {
		t.Fatal(err)
	}
	auth, err := NewAuth(authConfig, Config{Env: "local"}, logger)
	if err != nil {
		t.Fatal(err)
	}
	tenancyConfig, err := LoadTenancyConfig()
	if err != nil {
		t.Fatal(err)
	}
	tenancy, err := NewTenancy(tenancyConfig, Config{Env: "local"}, logger)
	if err != nil {
		t.Fatal(err)
	}

	var outgoing http.Header
	downstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		outgoing = r.Header.Clone()
		w.WriteHeader(http.StatusNoContent)
	}))
	defer downstream.Close()
	client := &http.Client{Transport: PropagatingTransport{}}

	var tenant string
	handler := Chain(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		tenant, _ = TenantFrom(r.Context())
		out, _ := http.NewRequestWithContext(r.Context(), http.MethodGet, downstream.URL, nil)
		response, err := client.Do(out)
		if err != nil {
			t.Error(err)
			return
		}
		response.Body.Close()
		w.WriteHeader(http.StatusOK)
	}), RequestID, Trace, Authenticate(auth), ResolveTenant(tenancy), RequireTenant)

	call := func(token string) int {
		tenant = ""
		request := httptest.NewRequest(http.MethodGet, "/cross", nil)
		request.Header.Set("Authorization", "Bearer "+token)
		request.Header.Set("traceparent", os.Getenv("CROSS_TRACEPARENT"))
		request.Header.Set("tracestate", os.Getenv("CROSS_TRACESTATE"))
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, request)
		return recorder.Code
	}

	result := map[string]any{}
	result["access_status"] = call(os.Getenv("CROSS_ACCESS_TOKEN"))
	result["tenant"] = tenant
	result["traceparent_out"] = outgoing.Get("traceparent")
	result["tracestate_out"] = outgoing.Get("tracestate")
	result["refresh_status"] = call(os.Getenv("CROSS_REFRESH_TOKEN"))
	result["expired_status"] = call(os.Getenv("CROSS_EXPIRED_TOKEN"))

	raw, _ := json.Marshal(result)
	if err := os.WriteFile(os.Getenv("CROSS_RESULT"), raw, 0o644); err != nil {
		t.Fatal(err)
	}
}
"""


def _run_go(root: Path, env: dict[str, str]) -> dict[str, Any]:
    assert GO is not None
    (root / "internal" / "jfast" / "crosslang_test.go").write_text(HARNESS, encoding="utf-8")
    command = ["go", "test", "-count=1", "-run", "^TestCrossLanguage$", "./internal/jfast/"]
    if GO:
        docker_env = [arg for key, value in env.items() for arg in ("-e", f"{key}={value}")]
        full = [
            *GO,
            "run",
            "--rm",
            "-v",
            f"{root}:/src",
            "-w",
            "/src",
            *docker_env,
            "-e",
            "CROSS_RESULT=/src/cross.json",
            GO_IMAGE,
            *command,
        ]
        completed = subprocess.run(full, capture_output=True, text=True, timeout=600, check=False)
    else:
        completed = subprocess.run(
            command,
            cwd=root,
            env={**os.environ, **env, "CROSS_RESULT": str(root / "cross.json")},
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result: dict[str, Any] = json.loads((root / "cross.json").read_text(encoding="utf-8"))
    return result


async def _python_side(auth: dict[str, Any], tokens: dict[str, str]) -> dict[str, Any]:
    """What a JFast app with the same auth and tenancy makes of the same tokens."""
    router = APIRouter()

    @router.get("/cross")
    async def cross(tenant: str = Depends(current_tenant)) -> dict[str, str]:
        return {"tenant": tenant}

    app = build_test_app(
        plugins=["observability", "auth", "tenancy"],
        routers=[router],
        raw={"plugin": {"auth": auth, "tenancy": {"sources": ["token"]}}},
    )
    seen: dict[str, Any] = {}
    async with client_for(app) as client:
        for name, token in tokens.items():
            response = await client.get("/cross", headers={"Authorization": f"Bearer {token}"})
            seen[f"{name}_status"] = response.status_code
            if response.status_code == 200:
                seen["tenant"] = response.json()["tenant"]
    return seen


async def _mint(key: Any, algorithm: str) -> dict[str, str]:
    """An access/refresh pair from the auth plugin's own issuer, and an expired token."""
    issuer = TokenIssuer(
        key=key,
        algorithm=algorithm,
        issuer=ISSUER,
        audience=AUDIENCE,
        access_lifetime=timedelta(minutes=15),
        refresh_lifetime=timedelta(days=30),
        store=MemoryTokenStore(),
        claims=TokenClaims(),
    )
    pair = await issuer.issue_pair("user-7", scopes=["items:write"], tenant_id="acme")
    expired, _, _ = issue(
        "user-7",
        key=key,
        algorithm=algorithm,
        lifetime=timedelta(minutes=-5),
        audience=AUDIENCE,
        issuer=ISSUER,
        tenant_id="acme",
    )
    return {"access": pair.access_token, "refresh": pair.refresh_token, "expired": expired}


def _rsa_pair() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


@needs_go
@pytest.mark.parametrize("mode", ["secret", "public_key"])
async def test_go_accepts_python_tokens_and_resolves_the_same_tenant(
    tmp_path: Path, mode: str
) -> None:
    if mode == "secret":
        algorithm, signing_key = "HS256", SECRET
        python_auth = {"mode": "secret", "algorithms": ["HS256"], "secret": SECRET}
        go_env = {"JFAST_AUTH_MODE": "secret", "JFAST_AUTH_SECRET": SECRET}
    else:
        private_pem, public_pem = _rsa_pair()
        algorithm, signing_key = "RS256", private_pem
        python_auth = {"mode": "public_key", "algorithms": ["RS256"], "public_key": public_pem}
        go_env = {"JFAST_AUTH_MODE": "public_key", "JFAST_AUTH_PUBLIC_KEY": public_pem}

    tokens = await _mint(signing_key, algorithm)
    python_auth.update({"issuer": ISSUER, "audience": AUDIENCE})
    python = await _python_side(python_auth, tokens)

    root = _generate(tmp_path / "edge")
    go = _run_go(
        root,
        {
            **go_env,
            # Same values, same names as the Python side reads.
            "JFAST_AUTH_ALGORITHMS": json.dumps([algorithm]),
            "JFAST_AUTH_ISSUER": ISSUER,
            "JFAST_AUTH_AUDIENCE": AUDIENCE,
            "JFAST_TENANCY_SOURCES": '["token"]',
            "CROSS_ACCESS_TOKEN": tokens["access"],
            "CROSS_REFRESH_TOKEN": tokens["refresh"],
            "CROSS_EXPIRED_TOKEN": tokens["expired"],
            "CROSS_TRACEPARENT": TRACEPARENT,
            "CROSS_TRACESTATE": TRACESTATE,
        },
    )

    # The same token, the same tenant, on both sides.
    assert python["access_status"] == 200
    assert go["access_status"] == 200
    assert go["tenant"] == python["tenant"] == "acme"
    # A refresh token is not a bearer and an expired token is no session:
    # 401 on both sides, so a client refreshes the same way against either.
    assert go["refresh_status"] == python["refresh_status"] == 401
    assert go["expired_status"] == python["expired_status"] == 401
    # The trace survives the Go hop untouched.
    assert go["traceparent_out"] == TRACEPARENT
    assert go["tracestate_out"] == TRACESTATE
