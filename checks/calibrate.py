import sys, itertools
sys.path.insert(0, ".")
from datetime import date
from alibi.feasibility import Horizon, Session, Task, analyze
D = [date(2026,10,d) for d in range(10,15)]
def hz():
    return Horizon(D, sessions=[Session("os_sat","OS",D[0],180,must_attend=True),
                                 Session("dbms_sat_lab","DBMS",D[0],180,must_attend=True),
                                 Session("os_mon","OS",date(2026,10,12),150),
                                 Session("os_tue","OS",date(2026,10,13),150)])
def tasks(x,y,z,a,b):
    return [Task("dbms_lab4","DBMS Lab 4 (normalisation + ERD)","DBMS",x,date(2026,10,11),
                weight=0.10, ext_days=3, ext_pct_per_day=10.0, released=date(2026,10,10)),
            Task("os_assign3","OS A3 (scheduling + deadlock report)","OS",y,date(2026,10,12),
                weight=0.10, released=date(2026,10,10)),
            Task("daa_assign4","DAA A4 (greedy + DP, 4 problems)","DAA",z,date(2026,10,16),
                weight=0.05, requires=("dbms_lab4",)),
            Task("os_ia2","OS IA2 prep","OS",a,date(2026,10,15),weight=0.15,review_blocks=2,review_gap_days=1),
            Task("dbms_ia2","DBMS IA2 prep","DBMS",b,date(2026,10,16),weight=0.15,review_blocks=2,review_gap_days=1)]
hits=[]
for x,y,z,a,b in itertools.product([300,360,420,480],[240,300,360],[180,240],[180,240],[180,240]):
    r = analyze(hz(), tasks(x,y,z,a,b), max_seconds=1.0)
    if r.status!="INFEASIBLE": continue
    ext_ok = any(m["kind"]=="extension_request" and m["makes_feasible"] for m in r.remedies)
    cap_ok = any(m["kind"]=="raise_work_cap" and m["makes_feasible"] for m in r.remedies)
    hits.append((x,y,z,a,b,ext_ok,cap_ok,r.core,len(r.why)))
    if ext_ok and not cap_ok:
        print(f"*** IDEAL  dbms={x} os={y} daa={z} ospb={a} dbmsp={b}")
        print("    core:", r.core); print("    shortfalls:", {k:v['slack_min'] for k,v in r.per_prefix.items()})
        sys.exit(0)
for h in hits[:14]: print(h)
print("total candidates found:", len(hits))
