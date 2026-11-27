"""Self-check for heartbeat checks, including /ping/<token>/fail. Run from host/:  python test_heartbeat.py"""
import os
import tempfile
from datetime import datetime, timedelta

os.environ.update(ADMIN_USERNAME="admin", ADMIN_PASSWORD="hunter2", SESSION_HTTPS_ONLY="false",
                  DATABASE_URL=f"sqlite:///{tempfile.mkdtemp()}/t.db", NTFY_URL="http://127.0.0.1:9/topic")
os.chdir(tempfile.mkdtemp())
os.symlink(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"), "app")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import monitor, notifier, problems  # noqa: E402
from app.database import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Device, Problem, ServiceCheck  # noqa: E402

SENT = []
notifier.send = lambda message, **kw: SENT.append(message) or True


async def _idle():
    return None


monitor.run_forever = _idle

with TestClient(app) as c:
    with Session(engine) as s:
        d = Device(name="willow", last_seen_at=datetime.utcnow(), offline_ok=True)  # only its checks matter here
        s.add(d)
        s.commit()
        s.add(ServiceCheck(device_id=d.id, name="Pearler sync", kind="heartbeat", interval_seconds=86400,
                           grace_seconds=4 * 86400, token="tok123"))
        s.commit()

    def state():
        with Session(engine) as s:
            monitor.check_heartbeats(s)
            problems.sync(s)
            ck = s.exec(select(ServiceCheck)).one()
            open_ = s.exec(select(Problem).where(Problem.resolved_at == None)).all()  # noqa: E711
            return ck.last_status, [p.title for p in open_]

    assert c.post("/ping/nope/fail", content=b"x").status_code == 404
    assert c.get("/ping/tok123").text == "ok\n"
    assert state() == ("up", []), state()
    # a failure with a reason: down, one problem with the reason, pushed once; it survives the sweep
    assert c.post("/ping/tok123/fail", content="Pearler needs signing in again".encode()).status_code == 200
    assert state() == ("down", ["Pearler sync: Pearler needs signing in again"]), state()
    assert state()[0] == "down" and len(SENT) == 1 and "signing in" in SENT[0], SENT
    # a new reason updates the same problem (no second push)
    c.post("/ping/tok123/fail", content=b"Pearler sign-in ends Wed 25 Feb")
    assert state() == ("down", ["Pearler sync: Pearler sign-in ends Wed 25 Feb"]) and len(SENT) == 1
    # an empty body still fails, with a generic reason
    c.post("/ping/tok123/fail")
    assert "reported a failure" in state()[1][0]
    # the next good ping clears it
    c.post("/ping/tok123")
    assert state() == ("up", []), state()
    # going quiet still raises "missed"
    with Session(engine) as s:
        ck = s.exec(select(ServiceCheck)).one()
        ck.last_checked_at = datetime.utcnow() - timedelta(days=6)
        s.add(ck)
        s.commit()
    st, titles = state()
    assert st == "down" and titles == ["Pearler sync missed"], titles
print("ok")
