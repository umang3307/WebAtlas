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
