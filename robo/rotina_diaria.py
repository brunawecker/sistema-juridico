#!/usr/bin/env python
"""Robô das 7h — a rotina diária que morava no Apps Script, agora na nuvem.

Roda em dia útil às 7h de Brasília (10h UTC) e faz, na ordem do original:
  1. Saneia tarefas concluídas presas com data no passado (avança pela frequência).
  2. Devolve ao dono as delegações órfãs (impulso sumiu sem concluir).
  3. Consolida o tempo de reuniões de dias anteriores no histórico.
  4. Cria as tarefas fixas do dia (diárias, semanais, fechamento mensal).
A fotografia diária (snapshot) fica com o robô de cobrança, que roda a cada 5 min.

Credenciais por variáveis de ambiente (segredos do GitHub): PG*.
"""
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

SP = ZoneInfo("America/Sao_Paulo")
HEADS = {"Danielly", "Bruna", "Eduarda"}


def novo_id(cur, prefixo, pad):
    cur.execute("update juridico.contadores set valor = valor + 1 where prefixo = %s returning valor",
                (prefixo,))
    return f"{prefixo}-{cur.fetchone()[0]:0{pad}d}"


def main():
    agora = datetime.now(SP)
    hoje = agora.date()
    if hoje.weekday() >= 5:
        print("fim de semana — nada a fazer")
        return 0
    hoje_br = hoje.strftime("%d/%m/%Y")
    hoje_curto = hoje.strftime("%d/%m/%y")
    mes_key = f"{hoje.year}-{hoje.month}"
    # primeiro dia útil do mês?
    prim = hoje.replace(day=1)
    while prim.weekday() >= 5:
        prim += timedelta(days=1)
    prim_util = (hoje == prim)

    with psycopg.connect() as conn, conn.cursor() as cur:
        # 1) concluídas presas com data no passado: avança pela frequência
        cur.execute("""
          update juridico.operacional set
            data_revisao_dt = %(hoje)s::date + case
              when upper(coalesce(check_,'')) like '%%DI_RIO%%' then 1
              when upper(coalesce(check_,'')) like '%%SEMANAL%%' then 7
              when upper(coalesce(check_,'')) like '%%MENSAL%%' then 30
              else 0 end,
            data_revisao = to_char(%(hoje)s::date + case
              when upper(coalesce(check_,'')) like '%%DI_RIO%%' then 1
              when upper(coalesce(check_,'')) like '%%SEMANAL%%' then 7
              when upper(coalesce(check_,'')) like '%%MENSAL%%' then 30
              else 0 end, 'DD/MM/YYYY')
          where status_tarefa like '%%EM DIA%%'
            and data_revisao_dt is not null and data_revisao_dt < %(hoje)s::date""",
            {"hoje": hoje})
        print(f"1. saneadas (data avançada pela frequência): {cur.rowcount}")

        # 2) delegações órfãs: DELEGADA sem subtarefa de impulso ativa → devolve
        cur.execute("""
          update juridico.operacional o set
            status_tarefa = 'AGUARDANDO',
            data_revisao_dt = %s::date, data_revisao = %s
          where o.status_tarefa like '%%DELEGADA%%'
            and not exists (
              select 1 from juridico.operacional s
              where s.supervisao like '%%IMPULSO de%%'
                and s.supervisao like '%%Origem: ' || o.id_tarefa || '%%')""",
            (hoje, hoje_br))
        print(f"2. delegações órfãs devolvidas: {cur.rowcount}")

        # 3) reuniões de dias anteriores sem TIMER no histórico
        cur.execute("""
          select id_reuniao, data_dt, assessor, titulo, cliente, id_cliente,
                 coalesce(duracao_min_num, 30)::int
          from juridico.reunioes
          where data_dt is not null and data_dt < %s::date
            and not exists (select 1 from juridico.historico h
                            where h.tipo = 'TIMER'
                              and h.texto like '%%Reunião ' || id_reuniao || ':%%')""",
            (hoje,))
        reunioes = cur.fetchall()
        for rid, d, assessor, titulo, cliente, id_cli, mins in reunioes:
            hid = novo_id(cur, "HIS", 5)
            texto = (f"Tempo registrado: {mins} min — 📅 Reunião {rid}: "
                     f"{titulo or 'Reunião'}" + (f" ({cliente})" if cliente else ""))
            cur.execute("""insert into juridico.historico
                (id_historico, id_tarefa, id_cliente, data, data_dt, autor, tipo, texto, origem)
                values (%s,'REUNIAO',%s,%s,%s,%s,'TIMER',%s,'NOVO')""",
                (hid, id_cli, d.strftime("%d/%m/%y"), d, assessor or "", texto))
        print(f"3. reuniões consolidadas no histórico: {len(reunioes)}")

        # 4) tarefas fixas do dia
        DIAS_SEMANA = {"SEMANAL_SEG": 0, "SEMANAL_TER": 1, "SEMANAL_QUA": 2,
                       "SEMANAL_QUI": 3, "SEMANAL_SEX": 4}
        cur.execute("""select id_fixa, titulo, frequencia, assessor, head_clientes,
                       coalesce(tempo_min_num,20)::int, criado_por,
                       coalesce(ultima_execucao,'')
                       from juridico.tarefas_fixas
                       where upper(coalesce(ativo,'')) = 'SIM' order by id_fixa""")
        criadas = 0
        for fid, titulo, freq, assessor, head_cl, tempo, criado_por, ult in cur.fetchall():
            freq = (freq or "").strip()
            if not head_cl and (criado_por or "").strip() in HEADS:
                head_cl = criado_por.strip()
            # já rodou hoje? (aceita qualquer formato de marca: '27/07/26',
            # '27/07/2026' ou o do Apps Script antigo: '2026-7 (27/07/26)')
            ja_hoje = (hoje_br in ult) or (hoje_curto in ult)
            deve, check, oper = False, "DIÁRIO", "Cobrança Diária"
            if freq == "DIARIA" and not ja_hoje:
                deve = True
            elif freq == "MENSAL_1DIA_UTIL" and prim_util and mes_key not in ult:
                deve, check, oper = True, "IMEDIATO", "Fechamento Mensal"
            elif freq in DIAS_SEMANA and hoje.weekday() == DIAS_SEMANA[freq] and not ja_hoje:
                deve, oper = True, "Tarefa Semanal"
            if not deve:
                continue
            oid = novo_id(cur, "OP", 4)
            cur.execute("""insert into juridico.operacional
                (id_tarefa, advogada, data_inclusao, cliente, check_, operacao,
                 assessor, status_tarefa, data_revisao, data_revisao_dt)
                values (%s,%s,%s,%s,%s,%s,%s,'TAREFA FIXA',%s,%s)""",
                (oid, head_cl or "", hoje_br, "📌 " + (titulo or ""), check, oper,
                 assessor or "", hoje_br, hoje))
            marca = hoje_br if freq != "MENSAL_1DIA_UTIL" else f"{mes_key} ({hoje_br})"
            cur.execute("""update juridico.tarefas_fixas set
                ultima_execucao = %s, ultima_execucao_dt = %s where id_fixa = %s""",
                (marca, hoje, fid))
            criadas += 1
        print(f"4. tarefas fixas criadas: {criadas}")

        # batimento (o Painel vigia)
        cur.execute("""insert into juridico.robo_status (nome, ultima) values ('rotina7h', now())
                       on conflict (nome) do update set ultima = excluded.ultima""")
        conn.commit()
    print("rotina diária concluída.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
