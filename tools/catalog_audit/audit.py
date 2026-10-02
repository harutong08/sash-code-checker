#!/usr/bin/env python3
"""サッシ呼称チェッカーのデータとメーカーWEBカタログの突き合わせ（定期点検用）

やること
  1. 新しい版のカタログが出ていないか確認する（YKK APW430／APW330・LIXIL TW）
  2. 現在リンクしているカタログのサイズ表ページを画像で取得してOCRし、
     ページごとに「カタログにあってデータにない呼称」「データにあってカタログにない呼称」を出す
  3. baseline.json（目視で確認済みのOCRノイズ）に載っている差分を除き、
     残った差分だけを report.md に書き出す

使い方
  apt install tesseract-ocr && pip install pytesseract pillow
  python3 tools/catalog_audit/audit.py            # 全部
  python3 tools/catalog_audit/audit.py --skip-ocr # 新版チェックだけ
  python3 tools/catalog_audit/audit.py --update-baseline  # 今回の差分を「確認済み」として記録

index.html は読むだけで、書き換えない。
"""
import argparse, concurrent.futures as cf, json, os, re, subprocess, sys, tempfile, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
WORK = tempfile.mkdtemp(prefix='catalog_audit_')

# 現在リンクしているカタログ（index.html の YKK_CATALOG_CODES / SN3600 と揃えること）
CATALOGS = {
    'APW430': dict(maker='ykk', code='XAAAA-H26-074-2', id='13483900000', pages=range(48, 132),
                   kw='APW 430', match=r'APW\s*430.*商品カタログ'),
    'APW330': dict(maker='ykk', code='XAAAA-H26-072-2', id='13483890000', pages=range(46, 142),
                   kw='APW 330', match=r'APW\s*330.*商品カタログ'),
    'TW': dict(maker='lixil', code='SN3600', id='18080870000', pages=range(60, 300),
               kw='TW防火戸', match=r'(ＴＷ|TW).*(ＴＷ|TW)\s*防火戸\s*商品カタログ'),
}
YKK = 'https://webcatalog.ykkap.co.jp/iportal'
LIXIL = 'https://webcatalog.lixil.co.jp/iportal'


def curl(url, out=None, cookie=None, save_cookie=None, follow=True):
    cmd = ['curl', '-sS', '--max-time', '120', '--retry', '5', '--retry-all-errors', '--retry-delay', '3']
    if follow: cmd.append('-L')
    if cookie: cmd += ['-b', cookie]
    if save_cookie: cmd += ['-c', save_cookie]
    cmd += ['-o', out or '-', url]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode: raise RuntimeError(f'curl失敗 {url}: {r.stderr.decode()[:200]}')
    return None if out else r.stdout.decode('utf-8', 'ignore')


def sessions():
    ykk = os.path.join(WORK, 'ykk.txt'); lx = os.path.join(WORK, 'lixil.txt')
    curl(f'{YKK}/CatalogDetail.do?catalogid=13483890000&volumeid=YKKAPDC1&designID=pro', out=os.devnull, save_cookie=ykk)
    # LIXILは openCatalogAuth を通すとAPIが使えるセッションになる（designIDはoldinter）
    curl('https://webcatalog.lixil.co.jp/cgi-bin/openCatalogAuth.cgi?c=SN3600&p=102', out=os.devnull, save_cookie=lx)
    return {'ykk': (YKK, 'YKKAPDC1', 'pro', ykk), 'lixil': (LIXIL, 'LXL13001', 'oldinter', lx)}


def specs(block):
    return dict(re.findall(r'<name>([^<]*)</name><order>\d+</order><value>([^<]*)</value>', block))


def check_new_editions(S):
    """同じ名前で、今リンクしているものより新しく登録されたカタログを探す"""
    found = []
    for key, c in CATALOGS.items():
        base, vol, design, ck = S[c['maker']]
        kw = urllib.parse.quote(c['kw'])
        xml = curl(f'{base}/webapi.do?api=getCatalogpageByFreeword&volumeid={vol}&kw={kw}'
                   f'&designID={design}&ofs=0&rs=80&hrs=1', cookie=ck)
        cats = {}
        for b in re.findall(r'<catalog rank.*?</catalog>', xml, re.S):
            cid = re.search(r'<id>(\d+)</id>', b).group(1)
            sp = specs(b); add = re.search(r'<adddate>([^<]*)', b).group(1)
            cats[cid] = (sp.get('カタログコード', ''), sp.get('ツール名') or sp.get('カタログ名称', ''),
                         sp.get('発行年月', ''), add)
        if c['id'] not in cats:
            found.append(f'- **{key}**: 現在リンクしているカタログ（{c["code"]}）が検索結果に出てこない。'
                         '公開終了・差し替えの可能性あり')
            continue
        cur_add = cats[c['id']][3]
        for cid, (code, name, ym, add) in cats.items():
            if cid != c['id'] and re.search(c['match'], name) and '業務用' not in name and add > cur_add:
                found.append(f'- **{key}**: 新しい版らしきカタログあり → {name}（{code}、{ym}発行、{add}登録）'
                             f'　現在: {c["code"]}（{cats[c["id"]][2]}）')
    return found


def page_urls(S, key):
    c = CATALOGS[key]; base, vol, design, ck = S[c['maker']]
    xml = curl(f'{base}/webapi.do?api=getCatalogpageInfo&volumeid={vol}&id={c["id"]}&designID={design}', cookie=ck)
    out = {}
    for p in re.findall(r'<page>.*?</page>', xml, re.S):
        n = re.search(r'<number>([^<]*)</number>', p).group(1)
        m = re.findall(r'<hd[^>]*>(https://[^<]*)</hd>', p)
        if m: out[n] = m[0].replace('&amp;', '&')
    if not out: raise RuntimeError(f'{key}: ページ一覧を取得できない（API仕様変更の可能性）')
    return out


def ocr_page(url, cookie, tw):
    import pytesseract
    from PIL import Image
    f = os.path.join(WORK, f'p{abs(hash(url))}.jpg')
    curl(url, out=f, cookie=cookie)
    im = Image.open(f).convert('L'); W, H = im.size
    h = im.resize((W // 2, H // 2)) if W > 2500 else im
    wl = '0123456789ABM' if tw else '0123456789'
    pat = r'\d{5,7}[ABM]?' if tw else r'\d{5,6}'
    s = set()
    # 縮小版と二値化版の和集合（片方だけだと枠線に接した数字を落とすことがある）
    for img in (h, h.point(lambda v: 255 if v > 160 else 0)):
        t = pytesseract.image_to_string(img, config=f'--psm 11 -c tessedit_char_whitelist={wl}')
        s |= {x for x in t.split() if re.fullmatch(pat, x)}
    os.remove(f)
    return s


def load_data():
    s = open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read()
    out = {}
    for name in ('DATA_APW', 'DATA_TW'):
        d = json.loads(re.search(r'const ' + name + r'\s*=\s*(\{.*?\});\n', s, re.S).group(1))
        for code, lst in d['codes'].items():
            for t, w, h in lst:
                T = d['types'][t]; ser = T['series']
                key = 'TW' if ser.startswith('TW') else ('APW430' if ('430' in ser or '431' in ser) else 'APW330')
                out.setdefault(key, {}).setdefault(str(T['page']), set()).add(code)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--skip-ocr', action='store_true')
    ap.add_argument('--update-baseline', action='store_true')
    ap.add_argument('--only', choices=list(CATALOGS))
    a = ap.parse_args()
    S = sessions()
    lines = ['# カタログ照合レポート', '']
    newed = check_new_editions(S)
    lines += ['## 新しい版のカタログ', ''] + (newed or ['- なし（現在リンクしている版が最新）']) + ['']
    print('\n'.join(lines))
    diffs = {}; failed = []
    if not a.skip_ocr:
        os.environ.setdefault('OMP_THREAD_LIMIT', '1')
        data = load_data()
        for key, c in CATALOGS.items():
            if a.only and key != a.only: continue
            urls = page_urls(S, key); ck = S[c['maker']][3]; tw = key == 'TW'
            dk = data.get(key, {})
            widths = {re.match(r'\d{3}', x).group() for pg in dk.values() for x in pg}
            heights = {x[3:] for pg in dk.values() for x in pg}
            ok = (lambda x: x[:3] in widths) if tw else (lambda x: x[:3] in widths and x[3:] in heights)
            res = {}
            with cf.ThreadPoolExecutor(4) as ex:
                fut = {ex.submit(ocr_page, urls[str(n)], ck, tw): str(n) for n in c['pages'] if str(n) in urls}
                for f in cf.as_completed(fut):
                    try: res[fut[f]] = f.result()
                    except Exception as e:  # 通信エラーなどは報告に残して続行
                        failed.append(f'{key} P{fut[f]}'); print(f'取得失敗 {key} P{fut[f]}: {e}', file=sys.stderr)
            for p in sorted(set(res) | set(dk), key=int):
                if p not in res and f'{key} P{p}' in failed: continue
                oc = {x for x in res.get(p, set()) if ok(x)}; dc = dk.get(p, set())
                if not dc and len(oc) < 4: continue  # 表のないページの拾い残し
                co, do = sorted(oc - dc), sorted(dc - oc)
                if co or do: diffs[f'{key}:{p}'] = {'catalog_only': co, 'data_only': do}
            print(f'{key}: {len(res)}ページ照合', file=sys.stderr)
        bpath = os.path.join(HERE, 'baseline.json')
        base = json.load(open(bpath, encoding='utf-8')) if os.path.exists(bpath) else {}
        if a.update_baseline and not failed:
            json.dump(diffs, open(bpath, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, sort_keys=True)
        lines += ['## カタログとデータの差分（確認済みのOCRノイズを除く）', '']
        n = 0
        for k, v in diffs.items():
            b = base.get(k, {})
            co = [x for x in v['catalog_only'] if x not in b.get('catalog_only', [])]
            do = [x for x in v['data_only'] if x not in b.get('data_only', [])]
            if co or do:
                n += 1; key, p = k.split(':')
                lines.append(f'- **{key} P{p}**')
                if co: lines.append(f'  - カタログにあってデータにない: {", ".join(co)}')
                if do: lines.append(f'  - データにあってこのページにない: {", ".join(do)}')
        if not n: lines.append('- なし')
        if failed: lines += ['', f'⚠️ 画像を取得できず照合できなかったページ: {", ".join(failed)}']
        lines += ['', '※OCRの読み違いが混じる。差分が出たら該当ページを画像で確認してから直すこと。'
                  '確認してノイズだったものは `--update-baseline` で記録する。']
    rep = os.path.join(HERE, 'report.md')
    open(rep, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    print('\n'.join(lines[lines.index('## カタログとデータの差分（確認済みのOCRノイズを除く）'):] if diffs or not a.skip_ocr else []))
    print(f'\nレポート: {rep}', file=sys.stderr)


if __name__ == '__main__':
    main()
