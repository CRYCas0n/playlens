import glob
import json
import os
import re

S = json.load(open("summaries.json", encoding="utf-8"))

corp = {}
for f in glob.glob("corpus2/*.txt"):
    key = os.path.basename(f)[:-4]
    ids = set()
    for line in open(f, encoding="utf-8"):
        m = re.match(r"\[([CU]\d\d)\]", line.strip())
        if m:
            ids.add(m.group(1))
    corp[key] = ids

FILLER = ["смешанные отзывы", "в целом неплох", "есть свои плюсы", "многим понравилось",
          "неоднозначн", "у игры есть как", "в целом положительн", "в целом отрицательн",
          "хорошая игра", "мнения разделились", "понравилось не всем"]

ASPECT = [r"бо[ея]в", r"сюжет", r"персонаж", r"\bмир", r"графи", r"производительн", r"кадр",
          r"баг", r"вылет", r"текстур", r"монетизац", r"микротранзакц", r"pay-to-win", r"pvp", r"pve",
          r"гринд", r"камер", r"управлен", r"темп", r"диалог", r"озвуч", r"кампан", r"мультиплеер",
          r"кооп", r"патч", r"обновлен", r"механик", r"сложност", r"подземель", r"карт", r"цен[аеуы]",
          r"стелс", r"платформинг", r"прогресс", r"\bии\b", r"интерфейс", r"режим", r"исследован",
          r"парирован", r"босс", r"крафт", r"пол[её]т", r"класс", r"мисси", r"активност",
          r"повествован", r"оптимизац", r"разрешен", r"обещан", r"функци", r"клиент", r"времен",
          r"лимит", r"реиграбельн", r"глубин", r"уровн", r"противник", r"оруж", r"найт-сити",
          r"морск", r"кораб", r"абордаж", r"перемещен", r"паркур", r"маги", r"заклинан", r"кастомизац",
          r"билд", r"отыгрыш", r"решени", r"выбор", r"последстви", r"атмосфер", r"художествен",
          r"анимац", r"постановк", r"письм", r"сцен", r"дизайн", r"локац", r"побочн", r"задани",
          r"квест", r"экосистем", r"забег", r"ultrahand", r"fuse", r"строительств", r"соединени",
          r"апскейл", r"лицензи", r"порог", r"выборк", r"распределен", r"агрегат", r"оценк",
          r"состояни", r"релиз", r"верси", r"платформ", r"подписк", r"стиль", r"визуальн", r"звук",
          r"саундтрек", r"музык", r"хранилищ", r"поручен", r"финал", r"\bакт", r"новизн", r"сиквел",
          r"дополнени", r"сери", r"формул", r"тайминг", r"защит", r"площадк", r"wnba", r"эффект",
          r"перепрохожден", r"прохожден", r"техническ", r"железе", r"консол", r"содержани",
          r"ожидани", r"идеолог", r"повестк", r"жанр", r"новичк", r"терпени", r"пустот", r"однообраз"]


def specific(c):
    lc = c.lower()
    if any(f in lc for f in FILLER):
        return False
    return any(re.search(a, lc) for a in ASPECT)


rows = []
blocks = []
for key, g in S.items():
    if key.startswith("_"):
        continue
    ids = corp.get(key, set())
    n_crit = g.get("n_critic", 0)
    n_ut = g.get("n_user_texts", 0)
    for side in ("critics", "users"):
        blk = g.get(side) or {}
        if blk.get("refused"):
            continue
        pool = n_crit if side == "critics" else n_ut
        need = 3 if pool >= 20 else 2
        per = {"positives": 0, "negatives": 0}
        for kind in ("positives", "negatives"):
            for c in blk.get(kind, []):
                ev = c["evidence"]
                bad = [e for e in ev if e not in ids]
                cited = len([e for e in ev if e in ids])
                sp = specific(c["claim"])
                ok = (not bad) and cited >= need and sp
                per[kind] += int(ok)
                rows.append({"game": key, "side": side, "kind": kind, "pool": pool, "need": need,
                             "cited": cited, "bad": len(bad), "specific": sp, "pass": ok,
                             "claim": c["claim"]})
        ov = blk.get("overall", "")
        ov_ok = bool(ov) and specific(ov) and not any(f in ov.lower() for f in FILLER)
        # Gollum users has an intentionally empty positives list
        need_pos = 2 if blk.get("positives") else 0
        block_ok = per["positives"] >= need_pos and per["negatives"] >= 2 and ov_ok
        blocks.append({"game": key, "side": side, "pos_ok": per["positives"],
                       "neg_ok": per["negatives"], "overall_ok": ov_ok, "pass": block_ok})

out = []
w = out.append
w("=" * 78)
w("PV1 — СОДЕРЖАТЕЛЬНОСТЬ И ТРАССИРУЕМОСТЬ")
w("=" * 78)
w("")
w("Уровень блока (то, что видит пользователь: одна сторона одной игры).")
w("Блок засчитан, если >=2 плюса и >=2 минуса имеют валидные ссылки,")
w("проходят порог подтверждений и названы конкретно, а вывод не водянист.")
w("")
bok = sum(b["pass"] for b in blocks)
w(f"  блоков всего : {len(blocks)}")
w(f"  засчитано    : {bok}  ({100*bok/len(blocks):.1f}%)")
w("")
for b in blocks:
    mark = "OK  " if b["pass"] else "FAIL"
    w(f"  {mark} {b['game'][:44]:<46}{b['side']:<9}+{b['pos_ok']} -{b['neg_ok']} вывод:{'ok' if b['overall_ok'] else 'нет'}")
w("")
w("-" * 78)
w("Уровень отдельного утверждения (строгий срез)")
tot = len(rows)
ok = sum(r["pass"] for r in rows)
big = [r for r in rows if r["pool"] >= 20]
small = [r for r in rows if r["pool"] < 20]
w(f"  утверждений всего            : {tot}")
w(f"  прошли полностью             : {ok}  ({100*ok/tot:.1f}%)")
w(f"  на корпусах >=20 отзывов     : {sum(r['pass'] for r in big)}/{len(big)}  ({100*sum(r['pass'] for r in big)/len(big):.1f}%)")
w(f"  на корпусах <20 отзывов      : {sum(r['pass'] for r in small)}/{len(small)}  ({100*sum(r['pass'] for r in small)/len(small):.1f}%)")
w(f"  битых ссылок                 : {sum(1 for r in rows if r['bad'])}")
w(f"  формулировок без конкретики  : {sum(1 for r in rows if not r['specific'])}")
w("")
w("Утверждения, не прошедшие строгий срез (третьи по счёту тезисы — самые слабые):")
for r in rows:
    if r["pass"]:
        continue
    why = []
    if r["bad"]:
        why.append("битая ссылка")
    if r["cited"] < r["need"]:
        why.append(f"подтверждений {r['cited']}<{r['need']}")
    if not r["specific"]:
        why.append("нет конкретики")
    w(f"  {r['game'][:40]:<42}{r['side'][:7]:<8}{r['kind'][:3]:<4}[{', '.join(why)}] {r['claim'][:52]}")

w("")
w("=" * 78)
w("PV2 — ОБЪЯСНЕНИЯ РАЗРЫВА КРИТИКИ/ИГРОКИ")
w("=" * 78)
gaps = [(k, v["gap"]) for k, v in S.items() if not k.startswith("_") and (v.get("gap") or {}).get("explanation")]
refus = [(k, v["gap"]) for k, v in S.items() if not k.startswith("_") and (v.get("gap") or {}) and not (v.get("gap") or {}).get("explanation")]
w(f"  объяснений сгенерировано : {len(gaps)}")
w(f"  отказов                  : {len(refus)}")
w("")
for k, gp in gaps:
    ids = corp.get(k, set())
    ev = gp.get("evidence", [])
    bad = [e for e in ev if e not in ids]
    both = any(e.startswith("C") for e in ev) and any(e.startswith("U") for e in ev)
    hedged = any(x in gp["explanation"].lower() for x in ["объясняется", "указывает", "отражает", "дополнительный фактор", "по-разному"])
    w(f"  {k[:44]:<46} ссылок {len(ev)-len(bad)}/{len(ev)}  обе стороны:{'да' if both else 'НЕТ'}  осторожная формулировка:{'да' if hedged else 'нет'}")

w("")
w("Отказы от вердикта по порогу (<20 текстов):")
for k, v in S.items():
    if k.startswith("_"):
        continue
    if (v.get("users") or {}).get("refused"):
        w(f"  {k[:44]:<46} текстов={v.get('n_user_texts')}  оценок={v.get('n_user_scores')}")

w("")
w("=" * 78)
w("ДЕФЕКТЫ ДАННЫХ, ЗАФИКСИРОВАННЫЕ ПРИ РАЗБОРЕ")
w("=" * 78)
from collections import Counter

c = Counter()
for k, v in S.items():
    if k.startswith("_"):
        continue
    for f in v.get("flags", []):
        c[f["type"]] += 1
for t, n in c.most_common():
    w(f"  {t:<34} {n}")

txt = "\n".join(out)
open("score_out.txt", "w", encoding="utf-8").write(txt)
print(txt)
