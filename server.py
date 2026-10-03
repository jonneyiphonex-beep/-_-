from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
import json
import os
import socket
from html import unescape

ROOT = Path(__file__).resolve().parent
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
HOST = os.environ.get('HOST', '0.0.0.0')
PORT = int(os.environ.get('PORT', '8000'))


def get_local_urls():
    urls = [f'http://localhost:{PORT}', f'http://127.0.0.1:{PORT}']
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(socket.gethostname(), None):
            if family == socket.AF_INET and sockaddr and sockaddr[0] not in {'127.0.0.1', '0.0.0.0'}:
                urls.append(f'http://{sockaddr[0]}:{PORT}')
    except OSError:
        pass
    seen = []
    for value in urls:
        if value not in seen:
            seen.append(value)
    return seen


def translate_text_text(text, source="auto", target="ar"):
    clean_text = (text or '').strip()
    if not clean_text:
        raise ValueError('لا يوجد نص لترجمته.')

    try:
        params = urlencode({
            'client': 'gtx',
            'sl': source,
            'tl': target,
            'dt': 't',
            'ie': 'UTF-8',
            'oe': 'UTF-8',
            'q': clean_text,
        })
        request = Request(
            f'https://translate.googleapis.com/translate_a/single?{params}',
            headers={'User-Agent': 'Mozilla/5.0'},
        )
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode('utf-8'))
        segments = []
        if isinstance(data, list) and data and isinstance(data[0], list):
            for item in data[0]:
                if isinstance(item, list) and item and isinstance(item[0], str):
                    segments.append(item[0])
        translated = ''.join(segments).strip()
        if translated:
            return translated
        raise ValueError('تعذرت ترجمة النص عبر الخدمة الرئيسية.')
    except Exception:
        params = urlencode({
            'q': clean_text,
            'langpair': 'autodetect|ar' if source == 'auto' else f'{source}|{target}',
        })
        request = Request(
            f'https://api.mymemory.translated.net/get?{params}',
            headers={'User-Agent': 'Mozilla/5.0'},
        )
        try:
            with urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode('utf-8'))
        except Exception as error:
            raise RuntimeError(f'تعذرت خدمة الترجمة: {error}')
        if data.get('responseStatus') != 200 or not data.get('responseData', {}).get('translatedText'):
            reason = data.get('responseDetails') or 'لم تُرجع خدمة الترجمة نصًا.'
            raise RuntimeError(reason)
        translated = data['responseData']['translatedText']
        return unescape(translated).strip()


class ReaderHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        if urlsplit(self.path).path == "/api/read":
            self.read_page()
            return
        if urlsplit(self.path).path == "/api/translate":
            self.translate_text()
            return
        super().do_GET()

    def read_page(self):
        values = parse_qs(urlsplit(self.path).query)
        target = values.get("url", [""])[0]
        parsed = urlsplit(target)
        hostname = (parsed.hostname or "").lower()

        if parsed.scheme not in {"http", "https"} or not hostname:
            self.send_json(400, {"error": "أدخل رابطًا صالحًا يبدأ بـ http أو https."})
            return
        if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
            self.send_json(400, {"error": "لا يمكن قراءة عناوين الشبكات المحلية."})
            return
        try:
            if not ip_address(hostname).is_global:
                self.send_json(400, {"error": "لا يمكن قراءة عناوين الشبكات الخاصة."})
                return
        except ValueError:
            pass

        request = Request(
            f"https://r.jina.ai/{target}",
            headers={"Accept": "text/plain", "User-Agent": "DhadReader/1.0"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                self.send_json(413, {"error": "الصفحة أكبر من الحد المسموح للقراءة."})
                return
        except HTTPError as error:
            self.send_json(502, {"error": f"تعذر جلب الصفحة (HTTP {error.code})."})
            return
        except (URLError, TimeoutError) as error:
            self.send_json(502, {"error": f"تعذر الاتصال بخدمة القراءة: {error.reason if isinstance(error, URLError) else 'انتهت مهلة الطلب'}."})
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def translate_text(self):
        values = parse_qs(urlsplit(self.path).query)
        text = values.get('q', [''])[0].strip()
        if not text:
            self.send_json(400, {'error': 'أدخل نصًا لترجمته.'})
            return

        source = values.get('source', ['auto'])[0] or 'auto'
        target = values.get('target', ['ar'])[0] or 'ar'
        try:
            translated = translate_text_text(text, source=source, target=target)
        except Exception as error:
            self.send_json(503, {'error': str(error) or 'تعذرت خدمة الترجمة مؤقتًا.'})
            return

        self.send_json(200, {'text': translated})

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), ReaderHandler)
    urls = get_local_urls()
    print('ض قارئ الويب يعمل على:')
    for url in urls:
        print(f' - {url}')
    print('استخدم عنوان الشبكة المحلية (مثل 192.168.x.x) إذا كنت تفتح الموقع من هاتف Android على نفس الشبكة.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
