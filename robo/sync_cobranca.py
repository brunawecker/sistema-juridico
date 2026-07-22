#!/usr/bin/env python
"""Robô de cobrança — roda na nuvem (GitHub Actions) a cada 5 minutos.

Lê a aba de cobrança do mês na planilha do Google (JULHO 26, AGOSTO 26...,
com fallback JUL 26 / COBRANÇA_ATUAL) e atualiza a tabela juridico.cobranca
no banco do site. Assim o financeiro segue preenchendo a planilha e o site
fica no máximo alguns minutos atrás.

Credenciais SOMENTE por variáveis de ambiente (segredos do GitHub):
  GOOGLE_SA_JSON  conteúdo do JSON da conta de serviço (leitora da planilha)
  SHEET_ID        id da planilha
  PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE/PGSSLMODE  banco
"""
import json
import os
import sys
import time
import urllib.parse
from datetime import date

import psycopg
import requests
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import AuthorizedSession

SHEET_ID = os.environ["SHEET_ID"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
MESES = ["JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO",
         "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO"]
ABREV = ["JAN", "FEV", "MAR", "ABR", "MAI", "JUN",
         "JUL", "AGO", "SET", "OUT", "NOV", "DEZ"]


def sessao_google():
    info = json.loads(os.environ["GOOGLE_SA_JSON"])
    cr = Credentials.from_service_account_info(info, scopes=SCOPES)
    return AuthorizedSession(cr)


def ler_aba(sess, aba):
    rng = urllib.parse.quote(f"'{aba}'")
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{rng}"
           f"?valueRenderOption=FORMATTED_VALUE&dateTimeRenderOption=FORMATTED_STRING")
    ultimo = None
    for tent in range(4):
        r = sess.get(url, timeout=120)
        if r.status_code == 200:
            return r.json().get("values", [])
        ultimo = f"HTTP {r.status_code}: {r.text[:200]}"
        time.sleep(2 * (tent + 1))
    raise RuntimeError(f"falha ao ler aba {aba}: {ultimo}")


def bater_coracao():
    """Registra que o robô rodou — o Painel do site vigia este registro."""
    with psycopg.connect() as conn, conn.cursor() as cur:
        cur.execute("""insert into juridico.robo_status (nome, ultima)
                       values ('cobranca', now())
                       on conflict (nome) do update set ultima = excluded.ultima""")
        conn.commit()


def main():
    sess = sessao_google()
    hoje = date.today()
    ano2 = str(hoje.year)[-2:]
    m = hoje.month - 1
    candidatas = [f"{MESES[m]} {ano2}", f"{ABREV[m]} {ano2}", "COBRANÇA_ATUAL"]

    valores, aba_usada = None, None
    for aba in candidatas:
        try:
            valores = ler_aba(sess, aba)
            aba_usada = aba
            break
        except Exception:
            continue
    if not valores:
        print(f"nenhuma aba do mês encontrada ({candidatas}) — nada a fazer")
        bater_coracao()
        return 0

    hdr_i = None
    for i, row in enumerate(valores[:12]):
        s = "|".join(str(x) for x in row).lower()
        if "cliente" in s and "cnpj" in s:
            hdr_i = i
            break
    if hdr_i is None:
        print(f"cabeçalho não achado na aba {aba_usada} — nada a fazer")
        bater_coracao()
        return 0
    hdr = [str(h).strip() for h in valores[hdr_i]]

    def idx(*nomes):
        for n in nomes:
            for i, h in enumerate(hdr):
                if h.upper() == n.upper():
                    return i
        for n in nomes:
            for i, h in enumerate(hdr):
                if n.upper() in h.upper():
                    return i
        return None

    i_cnpj = idx("CNPJ/CPF", "CNPJ")
    i_cli = idx("CLIENTE", "Coluna 2", "RAZÃO SOCIAL")
    if i_cli is None and i_cnpj:
        i_cli = i_cnpj - 1
    i_prod = idx("PRODUTO")
    i_tipo = idx("TIPO DE COBR")
    i_venc = idx("VENC", "PREVISTA")
    i_val = idx("R$ BRUTO", "VALOR")
    i_st = idx("STATUS", "PAGAMENTO")
    i_adv = idx("ADV")
    i_obs = idx("OBS")

    def cel(row, i):
        return str(row[i]).strip() if (i is not None and i < len(row)) else ""

    def num(v):
        v = v.replace("R$", "").strip()
        if "," in v:
            v = v.replace(".", "").replace(",", ".")
        try:
            return float(v)
        except Exception:
            return None

    def head_de(adv):
        a = adv.upper()
        if "DANIELLY" in a or a.startswith("DANI"):
            return "Danielly"
        if "BRUNA" in a:
            return "Bruna"
        if "EDUARDA" in a or a.startswith("DUDA"):
            return "Eduarda"
        return adv

    linhas_cob = []
    for row in valores[hdr_i + 1:]:
        cnpj = "".join(ch for ch in cel(row, i_cnpj) if ch.isdigit())
        if len(cnpj) < 11:
            continue
        st = cel(row, i_st).upper()
        status = ("PAGO" if st == "PAGO" else
                  "ATRASADO" if "VENC" in st or "INADIMPLEN" in st else
                  "PENDENTE" if ("AG" in st or st == "") else st)
        adv = cel(row, i_adv)
        linhas_cob.append((aba_usada, cel(row, i_cli), cel(row, i_cnpj),
                           cel(row, i_prod), cel(row, i_tipo), cel(row, i_venc),
                           num(cel(row, i_val)), status, adv, head_de(adv),
                           cel(row, i_obs)))

    if not linhas_cob:
        print(f"aba {aba_usada} sem lançamentos válidos — mantendo dados atuais")
        bater_coracao()
        return 0

    with psycopg.connect() as conn, conn.cursor() as cur:
        cur.execute("truncate table juridico.cobranca")
        with cur.copy("""copy juridico.cobranca
            (aba,cliente,cnpj_cpf,produto,tipo_cobr,data_venc,valor_bruto,
             status_pgto,adv,head,obs) from stdin""") as cp:
            for linha in linhas_cob:
                cp.write_row(linha)
        conn.commit()
    bater_coracao()
    print(f"ok: {len(linhas_cob)} lançamento(s) da aba '{aba_usada}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
