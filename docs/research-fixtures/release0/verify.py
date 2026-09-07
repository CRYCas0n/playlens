import glob
import json
import os
import re

S=json.load(open("summaries.json",encoding="utf-8"))
# index corpora
corp={}
for f in glob.glob("corpus2/*.txt"):
    key=os.path.basename(f)[:-4]
    items={}
    for line in open(f,encoding="utf-8"):
        m=re.match(r"\[([CU]\d\d)\]\s*(\S+)\s*\|\s*([^|]*)\|\s*(.*)", line.strip())
        if m:
            rid,score,mid,quote=m.groups()
            items[rid]={"score":score.strip(),"mid":mid.strip(),"quote":quote.strip()}
    corp[key]=items

MIN_SUPPORT=3
rows=[]; dossier=[]
for key,g in S.items():
    if key.startswith("_"): continue
    items=corp.get(key,{})
    n_user_texts=g.get("n_user_texts",0)
    thin = n_user_texts < 20
    for side in ("critics","users"):
        blk=g.get(side) or {}
        if blk.get("refused"):
            rows.append((key,side,"REFUSED",0,0,"", "отказ по порогу"))
            continue
        for kind in ("positives","negatives"):
            for c in blk.get(kind,[]):
                ev=c.get("evidence",[])
                missing=[e for e in ev if e not in items]
                ok=[e for e in ev if e in items]
                # bucket/polarity signal for user claims
                buckets=[items[e]["mid"] for e in ok] if side=="users" else []
                status="OK" if (not missing and len(ok)>=MIN_SUPPORT) else ("MISSING_REF" if missing else "WEAK_SUPPORT")
                rows.append((key,side,kind,len(ok),len(missing),status,c["claim"][:70]))
                dossier.append({"game":key,"side":side,"kind":kind,"claim":c["claim"],
                                "status":status,"missing":missing,
                                "evidence":[{"id":e,"score":items[e]["score"],"src":items[e]["mid"],
                                             "quote":items[e]["quote"][:200]} for e in ok]})
    gp=g.get("gap") or {}
    if gp.get("explanation"):
        ev=gp.get("evidence",[]); missing=[e for e in ev if e not in items]; ok=[e for e in ev if e in items]
        status="OK" if (not missing and len(ok)>=4) else ("MISSING_REF" if missing else "WEAK_SUPPORT")
        rows.append((key,"gap","explanation",len(ok),len(missing),status,gp["explanation"][:70]))
        dossier.append({"game":key,"side":"gap","kind":"explanation","claim":gp["explanation"],
                        "status":status,"missing":missing,
                        "evidence":[{"id":e,"score":items[e]["score"],"src":items[e]["mid"],
                                     "quote":items[e]["quote"][:200]} for e in ok]})

json.dump(dossier, open("evidence_dossier.json","w",encoding="utf-8"), ensure_ascii=False, indent=1)

print(f"{'game':<48}{'side':<9}{'kind':<12}{'cited':>6}{'bad':>5}  status")
print("-"*105)
for r in rows:
    print(f"{r[0]:<48}{r[1]:<9}{r[2]:<12}{r[3]:>6}{r[4]:>5}  {r[5]}")

tot=[r for r in rows if r[2] in ("positives","negatives","explanation")]
okc=[r for r in tot if r[5]=="OK"]
weak=[r for r in tot if r[5]=="WEAK_SUPPORT"]
miss=[r for r in tot if r[5]=="MISSING_REF"]
ref=[r for r in rows if r[2]=="REFUSED"]
print("\n" + "="*60)
print(f"claims total                : {len(tot)}")
print(f"  >= {MIN_SUPPORT} valid citations : {len(okc)}  ({100*len(okc)/len(tot):.1f}%)")
print(f"  weak (<{MIN_SUPPORT} citations)  : {len(weak)}")
print(f"  broken references         : {len(miss)}")
print(f"refusals (threshold rule)   : {len(ref)}")
