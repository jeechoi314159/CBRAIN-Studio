import json
import subprocess
from types import SimpleNamespace
import pytest
from cbrain_studio.suite import usb


def test_live_lease_reports_owner_and_becomes_available_after_release(monkeypatch):
    monkeypatch.setattr(usb,'process_app_name',lambda:'CBRAIN Studio')
    lease=usb.Lease('TEST-SUITE-OWNER')
    try:
        with pytest.raises(usb.DongleBusy,match='CBRAIN Studio') as exc:usb.Lease('TEST-SUITE-OWNER')
        assert 'PID ' in exc.value.owner
    finally:lease.close()
    # Metadata remaining on disk must never be mistaken for an active lock.
    replacement=usb.Lease('TEST-SUITE-OWNER');replacement.close()


def test_old_pid_lock_resolves_known_app_and_handles_unavailable_ps(monkeypatch):
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='/apps/CBRAIN Studio.app/Contents/MacOS/CBRAIN Studio\n'))
    assert usb.lease_owner('34739')=='CBRAIN Studio (PID 34739)'
    def denied(*a,**k):raise PermissionError('ps blocked')
    monkeypatch.setattr(subprocess,'run',denied)
    assert usb.lease_owner('34739')=='다른 CBRAIN 앱 (PID 34739)'
    assert usb.lease_owner(json.dumps({'pid':123,'app':'CBRAIN Device Setup'}))=='CBRAIN Device Setup (PID 123)'
    assert usb.lease_owner('invalid')=='다른 CBRAIN 앱'


def test_pool_counts_connected_but_busy_dongles_without_touching_them(monkeypatch):
    devices=[usb.Dongle('/fake/a','a','CBRAIN_Bridge_1'),usb.Dongle('/fake/b','b','CBRAIN_Bridge_2')]
    monkeypatch.setattr(usb,'dongles',lambda:devices)
    def occupied(d):raise usb.DongleBusy('CBRAIN Studio (PID 123)')
    monkeypatch.setattr(usb,'USBBridge',occupied)
    pool=usb.Pool();assert pool.scan()==[]
    assert len(pool.detected)==2 and len(pool.busy)==2 and not pool.bridges
    assert all('CBRAIN Studio' in e for e in pool.errors)


def test_pool_can_scan_free_dongle_and_clears_old_busy_state(monkeypatch):
    devices=[usb.Dongle('/fake/a','a','CBRAIN_Bridge_1'),usb.Dongle('/fake/b','b','CBRAIN_Bridge_2')]
    monkeypatch.setattr(usb,'dongles',lambda:devices)
    occupied={'a'}
    def bridge(d):
        if d.serial in occupied:raise usb.DongleBusy('CBRAIN Studio (PID 123)')
        row=usb.Candidate(d,1,1,b'abcdefg',-45,0)
        return SimpleNamespace(dongle=d,scan=lambda *_:[row],close=lambda:None)
    monkeypatch.setattr(usb,'USBBridge',bridge)
    pool=usb.Pool();assert len(pool.scan())==1 and len(pool.busy)==1
    occupied.clear();assert len(pool.scan())==2 and not pool.busy and not pool.errors
    pool.close()
