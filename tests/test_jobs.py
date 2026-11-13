import threading

from app.jobs import RefreshJobs


def test_start_is_single_flight_and_keeps_last_result():
    jobs = RefreshJobs()
    gate = threading.Event()
    calls = []

    def work():
        calls.append(1)
        gate.wait(5)
        return "done"

    first = jobs.start("k", work)
    second = jobs.start("k", work)
    assert first is second and first.running
    gate.set()
    for _ in range(100):
        if not jobs.is_running("k"):
            break
        threading.Event().wait(0.01)
    assert calls == [1]
    assert jobs.get("k").result == "done" and jobs.get("k").ok


def test_exception_marks_job_failed_but_finished():
    jobs = RefreshJobs()

    def boom():
        raise ValueError("nope")

    jobs.start("k", boom)
    for _ in range(100):
        if not jobs.is_running("k"):
            break
        threading.Event().wait(0.01)
    job = jobs.get("k")
    assert not job.running and job.error == "nope" and not job.ok


def test_run_blocking_skips_when_already_running():
    jobs = RefreshJobs()
    gate = threading.Event()
    jobs.start("all", lambda: gate.wait(5))
    assert jobs.run_blocking("all", lambda: "x") is None
    gate.set()
    for _ in range(100):
        if not jobs.is_running("all"):
            break
        threading.Event().wait(0.01)
    assert jobs.run_blocking("all", lambda: "x").result == "x"
