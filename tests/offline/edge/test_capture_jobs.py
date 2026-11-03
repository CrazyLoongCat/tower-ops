"""E08 采集任务管理 + 结果存储（离线）。

覆盖：submit 立即受理与去重 / CONFLICT / 有界 BUSY / 逐通道结果与集合失败 /
取消 / 超期 / 结果查询不隐式删除 / 重启换 service_epoch。
"""
from __future__ import annotations

import time

import pytest

from application.contracts.camera_v2 import ErrorCode
from edge_service.camera_registry import CameraRegistry, ChannelConfig
from edge_service.capture_jobs import (
    STATE_DONE,
    CaptureJobManager,
    JobRejected,
    payload_hash,
)
from edge_service.result_store import (
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_OK,
    ResultStore,
)

CONFIGS = [ChannelConfig("cam1", "SN-A", width=64, height=48),
           ChannelConfig("cam2", "SN-B", width=64, height=48)]


def _jobs(**kw) -> tuple[CaptureJobManager, CameraRegistry]:
    reg = CameraRegistry(CONFIGS, **{k: v for k, v in kw.items()
                                     if k == "enumerated"})
    reg.start_all()
    rest = {k: v for k, v in kw.items() if k != "enumerated"}
    return CaptureJobManager(reg, service_epoch=3, **rest), reg


def _submit(jobs, *, rid="r1", client="windows-main", cams=("cam1",),
            deadline_ms=2000, allow_partial=True):
    return jobs.submit(request_id=rid, client_id=client, channels=cams,
                       deadline_ms=deadline_ms, allow_partial=allow_partial)


# ---------------------------------------------------------------- 受理
def test_submit_returns_accepted_with_deterministic_id():
    jobs, _ = _jobs()
    t = _submit(jobs)
    assert t.state == "ACCEPTED"
    assert t.capture_id.startswith("cap-3-windows-main-r1-")
    assert jobs.results.get(t.capture_id) is None      # 受理时还没有结果


def test_dedup_same_key_and_payload_returns_same_capture_id():
    jobs, _ = _jobs()
    a = _submit(jobs, rid="same", cams=("cam1",))
    b = _submit(jobs, rid="same", cams=("cam1",))
    assert a.capture_id == b.capture_id


def test_same_request_id_different_payload_is_conflict():
    jobs, _ = _jobs()
    _submit(jobs, rid="dup", cams=("cam1",))
    with pytest.raises(JobRejected) as ei:
        _submit(jobs, rid="dup", cams=("cam1", "cam2"))
    assert ei.value.code is ErrorCode.CONFLICT


def test_submit_requires_channels_and_positive_deadline():
    jobs, _ = _jobs()
    with pytest.raises(JobRejected):
        _submit(jobs, cams=())
    with pytest.raises(JobRejected):
        _submit(jobs, deadline_ms=0)


# ---------------------------------------------------------------- 有界
def test_busy_when_pending_exceeds_limit():
    jobs, _ = _jobs(max_pending=1)
    _submit(jobs, rid="a")
    with pytest.raises(JobRejected) as ei:
        _submit(jobs, rid="b")
    assert ei.value.code is ErrorCode.BUSY


def test_busy_on_channel_queue_depth():
    jobs, _ = _jobs(queue_depth=1)
    _submit(jobs, rid="a", cams=("cam1",))
    with pytest.raises(JobRejected) as ei:
        _submit(jobs, rid="b", cams=("cam1",))
    assert ei.value.code is ErrorCode.BUSY
    _submit(jobs, rid="c", cams=("cam2",))         # 其它通道不受影响


def test_finished_jobs_do_not_occupy_queue():
    """已完成任务不得继续占用在途额度（否则会假 BUSY）。"""
    jobs, _ = _jobs(max_pending=1)
    _submit(jobs, rid="a")
    jobs.pump()
    _submit(jobs, rid="b")                          # 应被接受
    assert jobs.pump() == 1


# ---------------------------------------------------------------- 收集
def test_pump_collects_frames_per_channel():
    jobs, _ = _jobs()
    t = _submit(jobs, cams=("cam1", "cam2"))
    assert jobs.pump() == 1
    assert jobs.ticket(t.capture_id).state == STATE_DONE
    # 逐通道各一条结果, 互不覆盖
    assert len(jobs.results.all_for(t.capture_id)) == 2
    for ch in ("cam1", "cam2"):
        item = jobs.results.get(t.capture_id, ch)
        assert item is not None and item.status == STATUS_OK
        assert item.channel == ch and item.frame is not None


def test_collect_sets_ok_status_and_channel_epoch():
    jobs, reg = _jobs()
    t = _submit(jobs, cams=("cam1",))
    jobs.pump()
    item = jobs.results.get(t.capture_id)
    assert item.status == STATUS_OK
    assert item.channel == "cam1"
    assert item.channel_epoch == reg.actor("cam1").health().epoch


def test_no_partial_allows_failure_makes_whole_set_failed():
    """allow_partial=False 且存在失败通道 -> 整个集合失败（不得只报部分成功）。"""
    jobs, _ = _jobs(enumerated=["SN-A"])            # cam2 设备缺失
    t = _submit(jobs, cams=("cam1", "cam2"), allow_partial=False)
    jobs.pump()
    item = jobs.results.get(t.capture_id)
    assert item.status == STATUS_FAILED
    assert "失败" in (item.error or "")


def test_partial_allows_other_channel_to_succeed():
    jobs, _ = _jobs(enumerated=["SN-A"])
    t = _submit(jobs, cams=("cam1",), allow_partial=True)
    jobs.pump()
    assert jobs.results.get(t.capture_id).status == STATUS_OK


# ---------------------------------------------------------------- 取消/超期
def test_cancel_then_pump_records_cancelled():
    jobs, _ = _jobs()
    t = _submit(jobs, cams=("cam1",))
    assert jobs.cancel(t.capture_id) is True
    assert jobs.cancel(t.capture_id) is False        # 重复取消无效
    jobs.pump()
    assert jobs.results.get(t.capture_id).status == STATUS_CANCELLED
    assert jobs.ticket(t.capture_id).state == STATE_DONE


def test_expired_job_records_expired():
    jobs, _ = _jobs()
    t = _submit(jobs, cams=("cam1",), deadline_ms=1)
    time.sleep(0.02)
    jobs.pump()
    assert jobs.results.get(t.capture_id).status == STATUS_EXPIRED


def test_bump_epoch_clears_pending_and_increments():
    jobs, _ = _jobs()
    _submit(jobs, rid="old")
    new_epoch = jobs.bump_epoch()
    assert new_epoch == 4 and jobs.stats()["pending"] == 0


# ---------------------------------------------------------------- 结果存储
def test_result_query_does_not_delete_then_release_does():
    jobs, _ = _jobs()
    t = _submit(jobs, cams=("cam1",))
    jobs.pump()
    assert jobs.results.get(t.capture_id) is not None
    assert jobs.results.get(t.capture_id) is not None   # 查询不隐式删除
    assert jobs.results.has(t.capture_id)
    assert jobs.release(t.capture_id) is True
    assert jobs.results.get(t.capture_id) is None


def test_result_store_ttl_and_capacity_are_bounded():
    from edge_service.result_store import JobResult

    capacity_store = ResultStore(max_results=2)
    for i in range(4):
        capacity_store.put(JobResult(capture_id=f"c{i}", request_id="r",
                                     client_id="x", service_epoch=1, channel="cam1"))
    assert capacity_store.stats()["count"] == 2          # 容量淘汰

    ttl_store = ResultStore()
    ttl_store.put(JobResult(capture_id="expired", request_id="r", client_id="x",
                            service_epoch=1, channel="cam1",
                            created_monotonic_s=0.0, ttl_ms=1))
    assert ttl_store.expired("expired") is True
    assert ttl_store.get("expired") is not None          # 过期也先能查到
    assert ttl_store.evict_expired() == 1                # 显式清理才删除
    assert ttl_store.get("expired") is None


def test_payload_hash_is_order_insensitive():
    a = payload_hash(channels=("cam1", "cam2"), required_streams=("color",),
                     allow_partial=True)
    b = payload_hash(channels=("cam2", "cam1"), required_streams=("color",),
                     allow_partial=True)
    assert a == b
