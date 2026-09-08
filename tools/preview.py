"""Local panel preview: Python 3.10+, no Home Assistant or dependencies."""

import argparse
import ast
from datetime import datetime, timedelta
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "custom_components" / "eink_panel"


def weather_icon(condition):
    # Reuse the integration's pure SVG function without importing Home Assistant.
    tree = ast.parse((PANEL / "__init__.py").read_text(encoding="utf-8"))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "_weather_icon_svg")
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "icons", "exec"), namespace)
    return namespace["_weather_icon_svg"](condition)


class Preview(BaseHTTPRequestHandler):
    locked = True
    vacuum_state = "docked"

    def respond(self, body, content_type="application/json", status=200):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/":
            return self.respond((ROOT / "tools" / "preview.html").read_text(encoding="utf-8"), "text/html")
        if url.path == "/preview-version":
            return self.respond({"version": str([
                p.stat().st_mtime_ns for p in (
                    PANEL / "panel.html", PANEL / "manifest.json", PANEL / "__init__.py"
                )
            ])})
        if url.path == "/eink-panel":
            template = (PANEL / "panel.html").read_text(encoding="utf-8")
            version = json.loads((PANEL / "manifest.json").read_text(encoding="utf-8"))["version"]
            for token, value in {
                "__PANEL_KEY_JSON__": json.dumps("local-preview"),
                "__LOCK_LABEL_HTML__": escape("Домофон"),
                "__LOCK_LABEL_JSON__": json.dumps("Домофон"),
                "__PROJECT_VERSION_HTML__": escape(version),
            }.items():
                template = template.replace(token, value)
            return self.respond(template, "text/html")
        if url.path == "/eink-panel/status":
            now = datetime.now()
            return self.respond({
                "weather": {"code": "partlycloudy", "condition": "Переменная облачность",
                            "temperature": "24", "unit": "°C", "icon_slot": "current",
                            "range": "27° / 18°", "wind": "3,2 м/с (СЗ)"},
                "forecast": [{"day": ("пн", "вт", "ср", "чт", "пт", "сб", "вс")[hour.weekday()],
                              "time": hour.strftime("%H:00"), "temperature": str(25 - i) + "°",
                              "code": code, "icon_slot": "hourly-" + str(i)}
                             for i, code in enumerate(("sunny", "partlycloudy", "cloudy", "rainy", "clear-night"))
                             for hour in [now + timedelta(hours=i + 1)]],
                "indoor": {"temperature": {"value": "23.4", "unit": "°C"},
                           "humidity": {"value": "46", "unit": "%"}},
                "lock": {"state": "locked" if Preview.locked else "unlocked", "available": True,
                         "label": "Закрыт" if Preview.locked else "Открыт",
                         "action": "unlock" if Preview.locked else "lock"},
            })
        if url.path == "/eink-panel/vacuum":
            labels = {"docked": "На базе", "cleaning": "Уборка", "paused": "Пауза",
                      "idle": "Ожидание", "returning": "Возвращается на базу"}
            return self.respond({"configured": True, "available": True,
                                 "state": Preview.vacuum_state, "label": labels[Preview.vacuum_state],
                                 "actions": ["start", "pause", "stop", "return_to_base"]})
        if url.path.startswith("/eink-panel/weather-icon/"):
            condition = parse_qs(url.query).get("condition", ["cloudy"])[0]
            return self.respond(weather_icon(condition), "image/svg+xml")
        self.respond({"error": "Not found"}, status=404)

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path == "/eink-panel/lock":
            Preview.locked = not Preview.locked
        elif url.path == "/eink-panel/vacuum":
            action = parse_qs(url.query).get("action", [""])[0]
            states = {"start": "cleaning", "pause": "paused", "stop": "idle", "return_to_base": "returning"}
            if action not in states:
                return self.respond({"ok": False, "message": "Неизвестная команда"}, status=400)
            Preview.vacuum_state = states[action]
        else:
            return self.respond({"error": "Not found"}, status=404)
        self.respond({"ok": True, "message": "Демонстрационная команда выполнена"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open preview in your browser")
    args = parser.parse_args()
    server = HTTPServer(("127.0.0.1", args.port), Preview)
    address = f"http://127.0.0.1:{server.server_port}/"
    print(f"Local demo preview: {address} (Ctrl+C to stop)", flush=True)
    if args.open:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
