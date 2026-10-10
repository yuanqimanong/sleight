"""noVNC inspects immediate WebSocket prototypes; preserve the native shape."""
import pytest

from sleight import connect
from sleight.deploy.api.viewer import shim

from .conftest import serve_http

pytestmark = pytest.mark.integration


def test_vnc_shim_keeps_native_websocket_properties(live_endpoint):
    script = shim("/viewer/local/test")
    html = '<html><head>' + script + '</head><body><h1>shim</h1></body></html>'
    with serve_http({"index.html": html}) as port, connect(live_endpoint) as session:
        session.open(f"http://127.0.0.1:{port}/index.html")
        result = session.eval("""(()=>{const socket=new WebSocket('ws://'+location.host+'/api/profiles/test/vnc','binary');
          const names=[...Object.keys(socket),...Object.getOwnPropertyNames(Object.getPrototypeOf(socket))];
          const result={missing:['send','close','binaryType','onerror','onmessage','onopen','protocol','readyState'].filter(n=>!names.includes(n)),
                        path:new URL(socket.url).pathname,open:WebSocket.OPEN};socket.close();return result})()""")
        assert result == {"missing": [], "path": "/viewer/local/test/api/profiles/test/vnc", "open": 1}
