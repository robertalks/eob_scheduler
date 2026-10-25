"""request_status retry behaviour (no network: the paho client's publish is
stubbed and the response event is driven by hand)."""
from eob_lib import Bt725Client


def make():
    # __init__ doesn't connect (only Bt725LiveThermostat does), so this is safe.
    return Bt725Client("031608", "pw", request_timeout=0.05, request_retries=2)


def test_retries_then_gives_up():
    c = make()
    pubs = []
    c._client.publish = lambda *a, **k: pubs.append(1)
    assert c.request_status() == {}          # nothing ever replies
    assert len(pubs) == 3                     # first attempt + 2 retries


def test_succeeds_on_a_later_publish():
    c = make()
    pubs = []

    def publish(*a, **k):
        pubs.append(1)
        if len(pubs) == 2:                    # the device answers the 2nd request
            c.last_status = {"measured_temp_c": 21.0, "relay_on": True}
            c._status_event.set()
    c._client.publish = publish

    out = c.request_status()
    assert out["measured_temp_c"] == 21.0
    assert len(pubs) == 2                      # stopped as soon as it got a reply


def test_no_retries_when_configured_zero():
    c = Bt725Client("031608", "pw", request_timeout=0.05, request_retries=0)
    pubs = []
    c._client.publish = lambda *a, **k: pubs.append(1)
    assert c.request_status() == {}
    assert len(pubs) == 1
