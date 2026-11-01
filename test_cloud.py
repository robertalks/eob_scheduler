"""elbock_cloud against a local mock of the Elektrobock API (no real network).
Also checks that PascalCase and camelCase JSON are both accepted, since the
real API's casing isn't guaranteed."""
import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def make_server(casing="Pascal", token="JWT123"):
    def key(pascal, camel):
        return pascal if casing == "Pascal" else camel

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            if self.path != "/api/Auth/login":
                return self._json(404, {})
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            # accept either casing on the request too
            user = body.get("UserName") or body.get("userName")
            pw = body.get("Password") or body.get("password")
            if user != "me@example.com" or pw != "secret":
                return self._json(401, {"error": "bad login"})
            self._json(200, {key("AuthToken", "authToken"): token,
                             key("Id", "id"): 42439, key("Login", "login"): user})

        def do_GET(self):
            if self.path != "/api/UsersDevices/":
                return self._json(404, {})
            if self.headers.get("Authorization") != f"Bearer {token}":
                return self._json(401, {"error": "no token"})
            self._json(200, [{
                key("UniqueIdentifier", "uniqueIdentifier"): "31608",
                key("MqttPass", "mqttPass"): "s3cr3t-mqtt",
                key("DeviceType", "deviceType"): 2,
                key("Name", "name"): "Boiler",
            }])

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def cloud(monkeypatch):
    def _make(casing="Pascal"):
        srv, base = make_server(casing)
        monkeypatch.setenv("EOB_DATA_API", base)
        import elbock_cloud
        importlib.reload(elbock_cloud)   # picks up EOB_DATA_API
        return elbock_cloud, srv
    yield _make


@pytest.mark.parametrize("casing", ["Pascal", "camel"])
def test_fetch_credentials(cloud, casing):
    elbock_cloud, srv = cloud(casing)
    try:
        devices = elbock_cloud.fetch_credentials("me@example.com", "secret")
    finally:
        srv.shutdown()
    assert len(devices) == 1
    d = devices[0]
    assert d.unique_id == "031608"          # zero-padded to 6
    assert d.mqtt_password == "s3cr3t-mqtt"
    assert d.mqtt_username == "PTWIFI_031608"
    assert d.name == "Boiler"


def test_bad_password_raises(cloud):
    elbock_cloud, srv = cloud()
    try:
        with pytest.raises(elbock_cloud.CloudError) as e:
            elbock_cloud.fetch_credentials("me@example.com", "wrong")
    finally:
        srv.shutdown()
    assert "email and password" in str(e.value).lower()
