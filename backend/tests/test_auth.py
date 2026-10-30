"""Testes de autenticação, autorização, sessão e CSRF (contra MariaDB real)."""
from conftest import ORIGIN, PASSWORD, login, logged_in


def test_health_ok_with_database(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok", "database": "ok"}


def test_login_valid_sets_secure_httponly_cookie(client, admin):
    resp = login(client, "admin")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["user"] == {"id": admin.id, "name": "Admin Teste", "username": "admin", "role": "admin"}
    assert body["csrf_token"]
    cookie = resp.headers["Set-Cookie"]
    assert "mx_session=" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "password" not in resp.get_data(as_text=True).lower()


def test_secure_flag_is_set_when_cookie_secure_enabled(config, container, admin):
    import dataclasses
    from src.adapters.web.app import create_app
    secure_cfg = dataclasses.replace(config, cookie_secure=True)
    client = create_app(secure_cfg, container).test_client()
    cookie = login(client, "admin").headers["Set-Cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie


def test_login_username_is_case_insensitive(client, admin):
    assert login(client, "  ADMIN ").status_code == 200


def test_login_invalid_password_and_unknown_user_look_identical(client, admin):
    wrong = login(client, "admin", "senha-errada-123")
    unknown = login(client, "ninguem", "senha-errada-123")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.get_json()["error"]["code"] == unknown.get_json()["error"]["code"] == "invalid_credentials"
    assert wrong.get_json()["error"]["message"] == unknown.get_json()["error"]["message"]
    assert "Set-Cookie" not in wrong.headers


def test_login_inactive_user_rejected(client, container, admin):
    conn = container.db.connect(autocommit=True)
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET is_active = 0 WHERE id = %s", (admin.id,))
    conn.close()
    assert login(client, "admin").status_code == 401


def test_login_validation_errors_do_not_echo_password(client):
    resp = client.post("/api/auth/login", json={"username": "x", "password": "p" * 200},
                       headers={"Origin": ORIGIN})
    assert resp.status_code == 400
    assert "p" * 20 not in resp.get_data(as_text=True)
    assert client.post("/api/auth/login", data="nao-json", headers={"Origin": ORIGIN}).status_code == 400


def test_me_without_authentication_is_401(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.get_json()["error"]["code"] == "not_authenticated"


def test_me_returns_user_after_login(client, operator):
    login(client, "caixa")
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.get_json()["user"]["role"] == "operator"
    assert "password_hash" not in resp.get_data(as_text=True)


def test_admin_route_requires_authentication(client):
    assert client.get("/api/admin/ping").status_code == 401


def test_admin_route_forbidden_for_operator(client, operator):
    login(client, "caixa")
    resp = client.get("/api/admin/ping")
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "forbidden"


def test_admin_route_allowed_for_admin(client, admin):
    login(client, "admin")
    assert client.get("/api/admin/ping").status_code == 200


def test_logout_revokes_session(client, admin):
    headers = logged_in(client, "admin")
    old_cookie = client.get_cookie("mx_session").value
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    # Mesmo reenviando o cookie antigo manualmente, a sessão não vale mais.
    client.set_cookie("mx_session", old_cookie)
    assert client.get("/api/auth/me").status_code == 401


def test_session_expires_after_idle_timeout(client, admin, clock, config):
    login(client, "admin")
    clock.advance(minutes=config.session_idle_minutes + 1)
    assert client.get("/api/auth/me").status_code == 401


def test_session_expires_at_absolute_limit_even_if_active(client, admin, clock, config):
    login(client, "admin")
    # Atividade constante a cada 30 min mantém a ociosidade baixa...
    elapsed = 0
    while elapsed < config.session_absolute_hours * 60 - 30:
        clock.advance(minutes=30)
        elapsed += 30
        assert client.get("/api/auth/me").status_code == 200
    # ...mas o limite absoluto vence.
    clock.advance(minutes=31)
    assert client.get("/api/auth/me").status_code == 401


def test_deactivated_user_loses_existing_session(client, container, admin):
    login(client, "admin")
    conn = container.db.connect(autocommit=True)
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET is_active = 0 WHERE id = %s", (admin.id,))
    conn.close()
    assert client.get("/api/auth/me").status_code == 401


def test_set_password_revokes_sessions(client, container, admin):
    login(client, "admin")
    container.users.set_password("admin", "outra-senha-forte-1")
    assert client.get("/api/auth/me").status_code == 401
    assert login(client, "admin", "outra-senha-forte-1").status_code == 200


def test_invalid_cookie_is_rejected_and_cleared(client, admin):
    client.set_cookie("mx_session", "token-forjado")
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401
    assert "mx_session=;" in resp.headers["Set-Cookie"]


# ---------------- CSRF ----------------

def test_logout_without_csrf_token_is_rejected(client, admin):
    login(client, "admin")
    resp = client.post("/api/auth/logout", headers={"Origin": ORIGIN})
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "csrf_failed"
    assert client.get("/api/auth/me").status_code == 200  # sessão continua válida


def test_logout_with_wrong_csrf_token_is_rejected(client, admin):
    login(client, "admin")
    resp = client.post("/api/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": "errado"})
    assert resp.status_code == 403


def test_cross_origin_request_is_rejected_even_with_valid_token(client, admin):
    headers = logged_in(client, "admin")
    headers["Origin"] = "https://site-malicioso.example"
    resp = client.post("/api/auth/logout", headers=headers)
    assert resp.status_code == 403
    assert client.get("/api/auth/me").status_code == 200


def test_login_from_foreign_origin_is_rejected(client, admin):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD},
                       headers={"Origin": "https://site-malicioso.example"})
    assert resp.status_code == 403
    assert "Set-Cookie" not in resp.headers


def test_cross_site_fetch_metadata_is_rejected(client, admin):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD},
                       headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403


def test_csrf_token_from_another_session_is_rejected(app, admin, operator):
    a, b = app.test_client(), app.test_client()
    token_a = logged_in(a, "admin")["X-CSRF-Token"]
    logged_in(b, "caixa")
    resp = b.post("/api/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": token_a})
    assert resp.status_code == 403


# ---------------- Erros / headers ----------------

def test_unknown_route_returns_json_404(client):
    resp = client.get("/api/nao-existe")
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "not_found"


def test_internal_error_does_not_leak_details(app, client):
    @app.get("/api/_boom")
    def boom():
        raise RuntimeError("segredo-interno-no-stacktrace")

    app.config["PROPAGATE_EXCEPTIONS"] = False
    resp = client.get("/api/_boom")
    assert resp.status_code == 500
    text = resp.get_data(as_text=True)
    assert "segredo-interno" not in text and "Traceback" not in text
    assert resp.get_json()["error"]["request_id"]


def test_api_responses_are_not_cacheable(client):
    assert client.get("/api/health").headers["Cache-Control"] == "no-store"
