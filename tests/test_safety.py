import httpx
import pytest

from webatlas.config import CrawlConfig
from webatlas.crawler import Crawler
from webatlas.safety import UnsafeTargetError, check_host, is_public_ip


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254",
                                "::1", "fc00::1", "0.0.0.0", "::ffff:127.0.0.1", "100.64.0.1"])
def test_internal_addresses_are_not_public(ip):
    assert not is_public_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses(ip):
    assert is_public_ip(ip)


def resolver_for(mapping):
    async def resolve(host):
        if host not in mapping:
            raise OSError("NXDOMAIN")
        return mapping[host]
    return resolve


async def test_check_host_blocks_names_that_resolve_internally():
    resolve = resolver_for({"evil.test": ["93.184.216.34", "10.0.0.1"]})
    with pytest.raises(UnsafeTargetError):
        await check_host("evil.test", resolve)


async def test_check_host_blocks_literal_metadata_ip():
    with pytest.raises(UnsafeTargetError):
        await check_host("169.254.169.254", resolver_for({}))


async def test_check_host_allows_public_names():
    await check_host("good.test", resolver_for({"good.test": ["93.184.216.34"]}))


async def test_crawler_refuses_redirect_to_internal_address():
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "good.test" and request.url.path == "/":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(404)

    cfg = CrawlConfig(per_host_delay=0, retries=0, use_sitemaps=False, respect_robots=False)
    crawler = Crawler(cfg, transport=httpx.MockTransport(handler),
                      resolver=resolver_for({"good.test": ["93.184.216.34"]}))
    result = await crawler.crawl(["http://good.test/"])

    assert not any("169.254.169.254" in u for u in requested)
    assert result.failures and "non-public" in result.failures[0].reason


@pytest.mark.parametrize("ip", ["64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "::127.0.0.1", "::a00:1"])
def test_ipv6_wrappers_around_internal_ipv4_are_blocked(ip):
    # NAT64 and IPv4-compatible forms reach the embedded IPv4 address on the wire.
    assert not is_public_ip(ip)


def test_nat64_wrapping_a_public_ipv4_is_allowed():
    assert is_public_ip("64:ff9b::808:808")   # 8.8.8.8


async def test_dns_rebinding_is_caught_after_connecting(monkeypatch):
    """The guard's DNS lookup says 'public', but the socket really connects to 127.0.0.1."""
    import http.server
    import threading

    from webatlas.fetcher import Fetcher

    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"internal secret")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        lying_dns = resolver_for({"localhost": ["93.184.216.34"]})   # first answer: public
        cfg = CrawlConfig(per_host_delay=0, retries=0)
        async with Fetcher(cfg, resolver=lying_dns) as fetcher:
            with pytest.raises(UnsafeTargetError, match="non-public address 127.0.0.1"):
                await fetcher.get(f"http://localhost:{server.server_port}/")
    finally:
        server.shutdown()
