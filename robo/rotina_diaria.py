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
import re
import sys
from datetime import datetime, timedelta, date
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
        # rola_util: data que cairia em sáb/dom vai para segunda (Bruna, 20/09)
        cur.execute("""
          update juridico.operacional set status_tarefa = 'AGUARDANDO'
          where status_tarefa like '%%EM DIA%%'""",
            {"hoje": hoje})
        # ⚠️ LIÇÃO WCENE (22/09/2026, 162 tarefas invisíveis): EM DIA ✅ era o
        # status "saudável" da planilha antiga, mas aqui significa "concluída,
        # esconder" — e a versão anterior deste passo EMPURRAVA a data dessas
        # tarefas para a frente toda manhã, mantendo-as invisíveis para sempre.
        # Agora: qualquer EM DIA vira AGUARDANDO (com a data que tiver) — com
        # data futura dorme como concluída normal; vencida/na data, APARECE.
        print(f"1. acordadas (EM DIA → AGUARDANDO): {cur.rowcount}")

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

        # 2b) ESCALADA de correções — regra HÍBRIDA (Bruna, 20/08/2026):
        # correção que ficou com a HEAD e não foi corrigida no dia do envio
        # passa, na manhã seguinte, ao senior com a MENOR fila de correções
        # entre Madu, Malu e Ygor; empate é resolvido pela direção.
        # A DANIELLY fica FORA da escalada (20/08/2026): os pareceres dela
        # ela revisa diretamente — só Bruna e Eduarda escalam.
        ESCALADA = {"Bruna": "Malu", "Eduarda": "Madu"}
        cur.execute("""select nome_sistema from juridico.equipe
            where nome_sistema in ('Madu','Malu','Ygor')
              and upper(coalesce(status,''))='ATIVO'""")
        seniors = [r[0] for r in cur.fetchall()]
        # só LEVANTAMENTOS e PARECERES escalam (Bruna, 20/08/2026) —
        # os demais tipos ficam com a head até ela corrigir/puxar
        cur.execute("""select o.id_tarefa, o.id_cliente, o.correcao_head,
              to_char(o.correcao_data_dt,'DD/MM')
            from juridico.operacional o
            where coalesce(o.correcao_head,'') = any(%s)
              and o.correcao_data_dt is not null
              and o.correcao_data_dt < %s::date
              and (upper(coalesce(o.operacao,'')) like '%%LEVANTAMENTO%%'
                   or upper(coalesce(o.operacao,'')) like '%%PARECER%%')
            order by o.correcao_data_dt""",
            (list(ESCALADA.keys()), hoje))
        pendentes = cur.fetchall()
        escaladas = 0
        if pendentes and seniors:
            filas = {}
            for s_ in seniors:
                cur.execute("""select count(*) from juridico.operacional
                    where correcao_head=%s""", (s_,))
                filas[s_] = cur.fetchone()[0]
            for tid, idc, head_c, dt_env in pendentes:
                menor = min(filas[s_] for s_ in seniors)
                empatados = sorted(s_ for s_ in seniors if filas[s_] == menor)
                alvo_dir = ESCALADA.get(head_c)
                alvo = alvo_dir if alvo_dir in empatados else empatados[0]
                cur.execute("""update juridico.operacional set correcao_head=%s
                    where id_tarefa=%s""", (alvo, tid))
                hid = novo_id(cur, "HIS", 5)
                cur.execute("""insert into juridico.historico
                    (id_historico,id_tarefa,id_cliente,data,data_dt,autor,tipo,texto,origem)
                    values (%s,%s,%s,%s,%s,'Sistema','CORRECAO',%s,'SITE')""",
                    (hid, tid, idc, hoje_curto, hoje,
                     f"⏫ Correção escalada: enviada a {head_c} em {dt_env} e não "
                     f"corrigida no dia — vai para {alvo} (regra híbrida: menor fila "
                     f"de correções; empate resolvido pela direção). A head pode "
                     f"puxar de volta quando quiser."))
                filas[alvo] += 1
                escaladas += 1
        print(f"2b. correções escaladas (híbrido menor-fila): {escaladas}")

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

        # 3z) cartão de fixa CONCLUÍDO vira zumbi: o site grava AGUARDANDO e
        # o robô só enxerga 'TAREFA FIXA' — o zumbi ficava vencido para sempre,
        # aparecendo fora da janela (caso da Laura, 16/09/2026: DARFs). Some
        # com eles todo dia; o cartão oficial nasce/acorda no passo 4.
        # Impulsos delegados (copiam o título 📌) são preservados.
        cur.execute("""delete from juridico.operacional
            where cliente like '📌%%'
              and status_tarefa <> 'TAREFA FIXA'
              and coalesce(status_tarefa,'') not like 'DELEGADA%%'
              and coalesce(supervisao,'') not like '%%IMPULSO de%%'""")
        print(f"3z. cartões-zumbi de fixa removidos: {cur.rowcount}")

        # 3z2) fixa DESATIVADA ou reescrita (título novo) deixava o cartão
        # antigo vivo para sempre — aparecia "repetido" nas fixas do dia
        # (caso do João, 21/09/2026: DARFs meta 70→20 e planilha de prazos).
        # Cartão TAREFA FIXA sem fixa ATIVA de mesmo título e assessor → some.
        cur.execute("""delete from juridico.operacional o
            where o.status_tarefa = 'TAREFA FIXA'
              and o.cliente like '📌%%'
              and not exists (select 1 from juridico.tarefas_fixas f
                              where upper(coalesce(f.ativo,'')) = 'SIM'
                                and ('📌 ' || f.titulo) = o.cliente
                                and coalesce(f.assessor,'') = coalesce(o.assessor,''))""")
        print(f"3z2. cartões de fixas desativadas/reescritas removidos: {cur.rowcount}")

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
            deve, check, oper, superv = False, "DIÁRIO", "Cobrança Diária", ""
            if freq == "DIARIA" and not ja_hoje:
                deve = True
            elif freq == "MENSAL_1DIA_UTIL" and prim_util and mes_key not in ult:
                deve, check, oper = True, "IMEDIATO", "Fechamento Mensal"
            elif freq in DIAS_SEMANA and hoje.weekday() == DIAS_SEMANA[freq] and not ja_hoje:
                # fixa semanal nasce SEMANAL (antes ia DIÁRIO e confundia —
                # caso Marcio Motos/João, Bruna 21/09/2026)
                deve, check, oper = True, "SEMANAL", "Tarefa Semanal"
            elif freq == "JANELA_FIM_MES" and not ja_hoje:
                # um cartão por dia útil, da penúltima semana até a metade da
                # última: do dia (último-13) ao dia (último-4) de cada mês
                prox_mes = (hoje.replace(day=28) + timedelta(days=4)).replace(day=1)
                ultimo = (prox_mes - timedelta(days=1)).day
                if ultimo - 13 <= hoje.day <= ultimo - 4:
                    deve, oper = True, "Janela Fim de Mês"
                    superv = (f"Meta do dia: {tempo} min — ao atingir, registre o "
                              f"andamento, conclua e siga para outra tarefa. O cartão "
                              f"volta no próximo dia útil da janela (dia {ultimo-13} a "
                              f"{ultimo-4}). [meta:{tempo}min]")
                else:
                    # FORA da janela o cartão aberto dorme até ela abrir de novo
                    # (concluir um DIÁRIO puxa +1 dia e furava a janela — caso
                    # da Laura, 03/09/2026)
                    if hoje.day < ultimo - 13:
                        alvo = hoje.replace(day=ultimo - 13)
                    else:
                        u2 = ((prox_mes.replace(day=28) + timedelta(days=4))
                              .replace(day=1) - timedelta(days=1)).day
                        alvo = prox_mes.replace(day=u2 - 13)
                    cur.execute("""update juridico.operacional
                        set data_revisao = %s, data_revisao_dt = %s
                        where cliente = %s and assessor = %s
                          and status_tarefa = 'TAREFA FIXA'
                          and data_revisao_dt < %s""",
                        (alvo.strftime("%d/%m/%Y"), alvo,
                         "📌 " + (titulo or ""), assessor or "", alvo))
                    if cur.rowcount:
                        print(f"4. fixa '{titulo}' fora da janela → dorme até {alvo}")
            if not deve:
                continue
            # cartão pendente da MESMA fixa? reaproveita (puxa a data para
            # hoje) — nunca duplica; a Malu chegou a acumular 7 cópias
            cur.execute("""select id_tarefa from juridico.operacional
                where cliente = %s and assessor = %s and status_tarefa = 'TAREFA FIXA'
                limit 1""", ("📌 " + (titulo or ""), assessor or ""))
            aberto = cur.fetchone()
            if aberto:
                cur.execute("""update juridico.operacional set check_ = %s,
                    data_revisao = %s, data_revisao_dt = %s where id_tarefa = %s""",
                    (check, hoje_br, hoje, aberto[0]))
            else:
                oid = novo_id(cur, "OP", 4)
                cur.execute("""insert into juridico.operacional
                    (id_tarefa, advogada, data_inclusao, cliente, check_, operacao,
                     assessor, status_tarefa, data_revisao, data_revisao_dt, supervisao)
                    values (%s,%s,%s,%s,%s,%s,%s,'TAREFA FIXA',%s,%s,%s)""",
                    (oid, head_cl or "", hoje_br, "📌 " + (titulo or ""), check, oper,
                     assessor or "", hoje_br, hoje, superv))
            marca = hoje_br if freq != "MENSAL_1DIA_UTIL" else f"{mes_key} ({hoje_br})"
            cur.execute("""update juridico.tarefas_fixas set
                ultima_execucao = %s, ultima_execucao_dt = %s where id_fixa = %s""",
                (marca, hoje, fid))
            criadas += 1
        print(f"4. tarefas fixas criadas: {criadas}")

        # 5) membro INATIVO não fica com nada: fixas dele são desligadas e os
        # cartões-fantasma somem; o resto vai para o operacional ATIVO com
        # menor carga (com nota de auditoria em cada tarefa movida)
        cur.execute("""update juridico.tarefas_fixas f set ativo='NAO'
            from juridico.equipe e
            where e.nome_sistema=f.assessor and upper(coalesce(e.status,''))<>'ATIVO'
              and upper(coalesce(f.ativo,''))='SIM'""")
        fixas_off = cur.rowcount
        cur.execute("""delete from juridico.operacional o
            using juridico.equipe e
            where e.nome_sistema=o.assessor and upper(coalesce(e.status,''))<>'ATIVO'
              and (o.cliente like '📌%%' or o.status_tarefa='TAREFA FIXA')""")
        fantasmas = cur.rowcount
        cur.execute("""select o.id_tarefa, o.id_cliente, o.assessor, o.supervisao
            from juridico.operacional o
            join juridico.equipe e on e.nome_sistema=o.assessor
            where upper(coalesce(e.status,''))<>'ATIVO'""")
        orfaos = cur.fetchall()

        def menos_carregado(excluir):
            cur.execute("""select e.nome_sistema from juridico.equipe e
                where upper(coalesce(e.status,''))='ATIVO'
                  and e.cargo not ilike '%%head%%' and e.cargo not ilike '%%vendedor%%'
                  and e.cargo not ilike '%%outro time%%'
                  and e.nome_sistema <> all(%s)
                order by (select count(*) from juridico.operacional x
                          where x.assessor=e.nome_sistema) asc limit 1""",
                (list(excluir),))
            r = cur.fetchone()
            return r[0] if r else None

        movidas = 0
        for oid, idc, antigo, superv in orfaos:
            superv = superv or ""
            # impulso não pode voltar para quem pediu nem para o dono da origem
            evitar = {antigo}
            origem_id = None
            if "IMPULSO de" in superv:
                m = re.search(r"IMPULSO de (\S+)", superv)
                if m:
                    evitar.add(m.group(1))
                m = re.search(r"Origem: (OP-\d+)", superv)
                if m:
                    origem_id = m.group(1)
                    cur.execute("select assessor from juridico.operacional where id_tarefa=%s",
                                (origem_id,))
                    r = cur.fetchone()
                    if r and r[0]:
                        evitar.add(r[0])
            novo = menos_carregado(evitar) or menos_carregado({antigo})
            if not novo:
                break
            cur.execute("update juridico.operacional set assessor=%s where id_tarefa=%s", (novo, oid))
            hid = novo_id(cur, "HIS", 5)
            cur.execute("""insert into juridico.historico
                (id_historico,id_tarefa,id_cliente,data,data_dt,autor,tipo,texto,origem)
                values (%s,%s,%s,%s,%s,'Sistema','HISTORICO',%s,'SITE')""",
                (hid, oid, idc, hoje_br, hoje,
                 f"🔁 Reatribuída automaticamente de {antigo} (desligado da equipe) para {novo} — regra: operacional ativo com menor carga."))
            if origem_id:
                cur.execute("""update juridico.operacional set status_tarefa=%s
                    where id_tarefa=%s and status_tarefa like 'DELEGADA%%'""",
                    (f"DELEGADA 🤝 → {novo}", origem_id))
            movidas += 1

        # origens que ainda apontam 'DELEGADA → <desligado>': religa ao dono
        # atual do impulso; se o impulso sumiu, a tarefa volta ao titular
        cur.execute("""select o.id_tarefa, o.id_cliente, e.nome_sistema
            from juridico.operacional o
            join juridico.equipe e on o.status_tarefa like 'DELEGADA%%→ '||e.nome_sistema
            where upper(coalesce(e.status,''))<>'ATIVO'""")
        religadas = 0
        for oid, idc, antigo in cur.fetchall():
            cur.execute("""select id_tarefa, assessor from juridico.operacional
                where supervisao like %s and supervisao like '🤝 IMPULSO%%' limit 1""",
                (f"%Origem: {oid}%",))
            sub = cur.fetchone()
            if sub and sub[1]:
                cur.execute("update juridico.operacional set status_tarefa=%s where id_tarefa=%s",
                            (f"DELEGADA 🤝 → {sub[1]}", oid))
                texto = (f"🔁 Delegação atualizada: o impulso {sub[0]} estava com {antigo} "
                         f"(desligado) e agora está com {sub[1]}.")
            else:
                cur.execute("""update juridico.operacional set status_tarefa='AGUARDANDO'
                    where id_tarefa=%s""", (oid,))
                texto = (f"🔁 A delegação para {antigo} (desligado) foi desfeita — o impulso "
                         f"não existe mais; a tarefa volta à agenda do titular.")
            hid = novo_id(cur, "HIS", 5)
            cur.execute("""insert into juridico.historico
                (id_historico,id_tarefa,id_cliente,data,data_dt,autor,tipo,texto,origem)
                values (%s,%s,%s,%s,%s,'Sistema','HISTORICO',%s,'SITE')""",
                (hid, oid, idc, hoje_br, hoje, texto))
            religadas += 1
        print(f"5. saída de membro: {fixas_off} fixa(s) desligada(s), "
              f"{fantasmas} cartão(ões)-fantasma removido(s), {movidas} tarefa(s) reatribuída(s), "
              f"{religadas} delegação(ões) religada(s)")

        # 6) higiene de vínculo cartão×cadastro (caso VIALIM×VIACON, 18/08/2026):
        # nome/CNPJ do cartão seguem o cadastro vinculado; se o NOME do cartão
        # bater com OUTRO cadastro, é suspeita de vínculo errado — o robô NÃO
        # religa sozinho: grava um alerta no cartão para as heads decidirem.
        import unicodedata

        def nrm_nome(s):
            s = unicodedata.normalize("NFD", str(s or "").upper())
            s = "".join(c for c in s if unicodedata.category(c) != "Mn")
            s = re.sub(r"\b(LTDA|EIRELI|ME|EPP|SA|S/A|CIA|&)\b", " ", s)
            s = re.sub(r"^\s*\d{1,2}\s+", "", s)   # prefixo de ordenação "01 "
            return re.sub(r"[^A-Z0-9]", "", s)

        cur.execute("select id_cliente, nome, cnpj_cpf from juridico.clientes")
        cad = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        por_nome = {}
        for cid, (nm, _) in cad.items():
            por_nome.setdefault(nrm_nome(nm), []).append(cid)
        cur.execute("""select id_tarefa, id_cliente, cliente from juridico.operacional
            where coalesce(id_cliente,'')<>'' and not cliente like '📌%%'""")
        sincronizados, suspeitos = 0, 0
        for tid, cid, cli in cur.fetchall():
            if cid not in cad:
                continue
            nome_cad, doc_cad = cad[cid]
            if nrm_nome(cli) == nrm_nome(nome_cad):
                if cli != nome_cad:
                    cur.execute("""update juridico.operacional set cliente=%s, cnpj_cpf=%s
                        where id_tarefa=%s""", (nome_cad, doc_cad, tid))
                    sincronizados += 1
                continue
            outros = [x for x in por_nome.get(nrm_nome(cli), []) if x != cid]
            if len(outros) == 1:
                cur.execute("""select 1 from juridico.historico where id_tarefa=%s
                    and texto like '⚠️ Possível vínculo errado%%' limit 1""", (tid,))
                if cur.fetchone():
                    continue
                hid = novo_id(cur, "HIS", 5)
                cur.execute("""insert into juridico.historico
                    (id_historico,id_tarefa,id_cliente,data,data_dt,autor,tipo,texto,origem)
                    values (%s,%s,%s,%s,%s,'Sistema','HISTORICO',%s,'SITE')""",
                    (hid, tid, cid, hoje_curto, hoje,
                     f"⚠️ Possível vínculo errado: o nome deste cartão ({cli}) bate com o "
                     f"cadastro {outros[0]}, mas o cartão está ligado a {cid} ({nome_cad}). "
                     f"Head: confira e corrija o vínculo (o robô não religa sozinho)."))
                suspeitos += 1
        print(f"6. vínculo cartão×cadastro: {sincronizados} nome(s)/CNPJ sincronizado(s), "
              f"{suspeitos} suspeita(s) sinalizada(s)")

        # 7) fechamento comercial: nos primeiros dias úteis do mês, congela a
        # foto do mês anterior (atingido líquido, novos × recorrência, projeção
        # final) — vira o "retrovisor" do Comercial. Não sobrescreve sementes.
        if hoje.day <= 5:
            mes_ant = (hoje.replace(day=1) - timedelta(days=1)).replace(day=1)
            cur.execute("""select
                (select count(*) from juridico.comercial_pagamentos where mes=%s),
                (select count(*) from juridico.comercial_fechamento
                  where mes=%s and origem='FOTO')""", (mes_ant, mes_ant))
            tem, ja = cur.fetchone()
            if tem and not ja:
                cur.execute("""insert into juridico.comercial_fechamento
                    (mes,pessoa,meta,projecao,atingido,novos,n_novos,recorrencia,n_rec,origem)
                    select %s, p.credito,
                      (select m.meta from juridico.config_metas m
                        where m.mes=%s and m.pessoa=p.credito),
                      coalesce(sum(p.valor_bruto) filter (where p.status<>'PAGO'
                        and coalesce(p.status,'')<>'' and not p.fora_projecao
                        and p.status not like '%%CANCEL%%'),0),
                      coalesce(sum(p.valor_liquido) filter (where p.status='PAGO'),0),
                      coalesce(sum(p.valor_liquido) filter (where p.status='PAGO' and p.tipo='NOVO'),0),
                      count(*) filter (where p.status='PAGO' and p.tipo='NOVO'),
                      coalesce(sum(p.valor_liquido) filter (where p.status='PAGO' and p.tipo<>'NOVO'),0),
                      count(*) filter (where p.status='PAGO' and p.tipo<>'NOVO'),
                      'FOTO'
                    from juridico.comercial_pagamentos p
                    where p.mes=%s and p.credito is not null
                    group by p.credito
                    on conflict (mes,pessoa) do nothing""", (mes_ant, mes_ant, mes_ant))
                print(f"7. fechamento comercial de {mes_ant}: {cur.rowcount} pessoa(s)")

        # 9) ESTEIRA DO CONTRATO NOVO (Bloco D, 16/09/2026): 1ª parcela paga
        # de NOVO → cartão "Parecer de Novo Cliente" (prazo fatal +7d) para o
        # assessor com menos imediatas. Desarmada até a Bruna validar o ensaio
        # (armar = insert esteira_novos id_lanc='__ARMADA__').
        cur.execute("select 1 from juridico.esteira_novos where id_lanc='__ARMADA__'")
        armada = cur.fetchone() is not None
        cur.execute("""
          with prim as (select id_lanc, min(data_pgto) d1 from juridico.comercial_pagamentos
            where status='PAGO' and tipo='NOVO' and coalesce(id_lanc,'')<>'' and data_pgto is not null
            group by id_lanc)
          select p.id_lanc, p.cliente, p.cnpj_cpf, p.credito, p.secao_head,
                 p.valor_liquido, p.data_pgto
          from juridico.comercial_pagamentos p
          join prim on prim.id_lanc=p.id_lanc and prim.d1=p.data_pgto
          where p.status='PAGO' and p.tipo='NOVO' and p.data_pgto >= current_date - 7
            and not exists (select 1 from juridico.esteira_novos e where e.id_lanc=p.id_lanc)
          order by p.data_pgto""")
        novos_est = cur.fetchall()
        for (lanc, cli, cnpj, cred, sec, vliq, dpg) in novos_est:
            if not armada:
                print(f"9. esteira (ENSAIO — desarmada): criaria parecer p/ {cli} ({lanc})")
                continue
            # assessor com menos imediatas abertas (Madu fora — só recebe de head)
            cur.execute("""
              select e.nome_sistema from juridico.equipe e
              where upper(coalesce(e.status,''))='ATIVO'
                and e.cargo not ilike '%%head%%' and e.cargo not ilike '%%vendedor%%'
                and e.nome_sistema <> 'Madu'
                and (e.ausente_ate is null or e.ausente_ate < current_date)               order by (select count(*) from juridico.operacional o
                        where o.assessor=e.nome_sistema
                          and upper(coalesce(o.check_,''))='IMEDIATO'
                          and o.status_tarefa not ilike '%%EM DIA%%'
                          and coalesce(o.correcao_head,'')=''),
                       (select count(*) from juridico.operacional o2
                        where o2.assessor=e.nome_sistema) limit 1""")
            alvo = (cur.fetchone() or ["João"])[0]
            cur.execute("""select id_cliente, head from juridico.clientes
              where cnpj_cpf=%s or upper(nome)=upper(%s) limit 1""", (cnpj or "", cli))
            r = cur.fetchone()
            idc, headc = (r[0], r[1]) if r else (None, sec or "")
            oid = novo_id(cur, "OP", 4)
            # regra: 7 dias após o pagamento; leva atrasada de 1º arranque
            # nunca nasce já estourada — mínimo de 2 dias úteis à frente
            fatal = max(dpg + timedelta(days=7), date.today() + timedelta(days=2))
            sup = (f"🧾 ESTEIRA DE NOVO CLIENTE — 1ª parcela paga em "
                   f"{dpg.strftime('%d/%m/%Y')} (R$ {vliq:.2f} líq., crédito {cred}). "
                   f"Fazer o parecer de boas-vindas do novo contrato e, com ele "
                   f"pronto, AGENDAR a reunião de 1º atendimento — a conclusão "
                   f"do cartão pede essa confirmação. Prazo máximo: 7 dias.")
            cur.execute("""insert into juridico.operacional
                (id_tarefa, id_cliente, advogada, data_inclusao, data_inclusao_dt,
                 cliente, cnpj_cpf, check_, operacao, assessor, status_tarefa,
                 data_revisao, data_revisao_dt, prazo_fatal, prazo_fatal_dt, supervisao)
                values (%s,%s,%s,to_char(current_date,'DD/MM/YYYY'),current_date,
                        %s,%s,'IMEDIATO','Parecer de Novo Cliente',%s,'AGUARDANDO',
                        to_char(current_date,'DD/MM/YYYY'),current_date,
                        to_char(%s::date,'DD/MM/YYYY'),%s,%s)""",
                (oid, idc, headc or (sec or ""), cli, cnpj or "", alvo, fatal, fatal, sup))
            hid = novo_id(cur, "HIS", 5)
            cur.execute("""insert into juridico.historico
                (id_historico,id_tarefa,id_cliente,data,data_dt,autor,tipo,texto,origem)
                values (%s,%s,%s,to_char(current_date,'DD/MM/YY'),current_date,
                        'Sistema','SUPERVISÃO',%s,'ROBO')""", (hid, oid, idc, sup))
            cur.execute("""insert into juridico.esteira_novos (id_lanc, id_tarefa, cliente)
                values (%s,%s,%s)""", (lanc, oid, cli))
            print(f"9. esteira: {oid} Parecer de Novo Cliente → {alvo} ({cli}, fatal {fatal})")
        if not novos_est:
            print("9. esteira: nenhum contrato novo pendente")

        # 10) TURNOS MENSAIS DAS HEADS (Eduarda 11, 16/09/2026): DRE + ajustes
        # de comissão e correção do relatório, prazo fatal dia 8, em rodízio
        # entre as 3 heads (uma no DRE, outra no relatório, a terceira livre).
        # Criados uma vez por mês (dia 1 a 8) e o turno é reservado na agenda.
        if hoje.day <= 8:
            ordem = ["Bruna", "Danielly", "Eduarda"]
            m = hoje.month
            dre_head = ordem[(m - 1) % 3]
            rel_head = ordem[m % 3]
            livre = ordem[(m + 1) % 3]
            # dia do turno = primeiro dia útil do mês, de manhã
            turno = prim
            fatal = hoje.replace(day=8)
            mesnome = hoje.strftime("%m/%Y")
            fixos = [
                ("📊 DRE + ajustes de comissão", dre_head,
                 f"Fechar o DRE e os ajustes de comissão do mês (prazo fatal dia 8). "
                 f"Turno reservado na agenda: {turno.strftime('%d/%m')} de manhã. "
                 f"Rodízio deste mês — DRE: {dre_head} · relatório: {rel_head} · "
                 f"livre p/ apoiar o time: {livre}."),
                ("📝 Correção do relatório", rel_head,
                 f"Revisar/corrigir o relatório do mês (prazo fatal dia 8). "
                 f"Turno reservado na agenda: {turno.strftime('%d/%m')} de manhã. "
                 f"Rodízio deste mês — DRE: {dre_head} · relatório: {rel_head} · "
                 f"livre p/ apoiar o time: {livre}."),
            ]
            # limpa turnos de meses ANTERIORES ainda abertos (admin efêmero)
            cur.execute("""delete from juridico.reunioes r
                using juridico.operacional o
                where o.operacao = 'Fechamento Mensal (heads)'
                  and o.cliente not like %s
                  and r.assessor = o.assessor and r.titulo like '🔒%%'""",
                (f"%({mesnome})",))
            cur.execute("""delete from juridico.operacional
                where operacao = 'Fechamento Mensal (heads)'
                  and cliente not like %s""", (f"%({mesnome})",))
            criados_dre = 0
            for titulo, quem, sup in fixos:
                cli = f"{titulo} ({mesnome})"
                cur.execute("""select 1 from juridico.operacional
                    where cliente = %s and assessor = %s""", (cli, quem))
                if cur.fetchone():
                    continue
                oid = novo_id(cur, "OP", 4)
                cur.execute("""insert into juridico.operacional
                    (id_tarefa, advogada, data_inclusao, data_inclusao_dt, cliente,
                     check_, operacao, assessor, status_tarefa, data_revisao,
                     data_revisao_dt, prazo_fatal, prazo_fatal_dt, supervisao)
                    values (%s,%s,to_char(current_date,'DD/MM/YYYY'),current_date,%s,
                            'MENSAL','Fechamento Mensal (heads)',%s,'AGUARDANDO',
                            to_char(current_date,'DD/MM/YYYY'),current_date,
                            to_char(%s::date,'DD/MM/YYYY'),%s,%s)""",
                    (oid, quem, cli, quem, fatal, fatal, sup))
                # reserva o turno na agenda (aparece como ocupada p/ quem delega)
                rid = novo_id(cur, "REU", 4)
                cur.execute("""insert into juridico.reunioes
                    (id_reuniao, data, data_dt, assessor, titulo, horario, duracao_min, obs)
                    values (%s, to_char(%s::date,'DD/MM/YYYY'), %s, %s, %s, '09:00', 240,
                            'Turno reservado — fechamento mensal das heads')""",
                    (rid, turno, turno, quem, "🔒 " + titulo))
                criados_dre += 1
            if criados_dre:
                print(f"10. turnos das heads criados: {criados_dre} (DRE {dre_head}, relatório {rel_head})")
            else:
                print("10. turnos das heads: já criados neste mês")

        # 8) janelas públicas congeladas (família do defeito de 19/08/2026):
        # tabela ganhou coluna nova e a view espelho "select *" não a expôe —
        # o site quebra em silêncio. Detecta e recria a view no ato.
        cur.execute("""
          with v as (select table_name vw, array_agg(column_name::text) cols
                     from information_schema.columns where table_schema='public' group by 1),
               t as (select table_name tb, array_agg(column_name::text) cols
                     from information_schema.columns where table_schema='juridico' group by 1)
          select v.vw from v join t on t.tb = v.vw
          where array(select unnest(t.cols) except select unnest(v.cols)) <> '{}'""")
        congeladas = [r[0] for r in cur.fetchall()]
        for vw in congeladas:
            try:
                cur.execute(f"""create or replace view public.{vw}
                    with (security_invoker=true) as select * from juridico.{vw}""")
                print(f"8. janela pública {vw} descongelada (colunas novas expostas)")
            except Exception as e:
                print(f"8. AVISO: não consegui descongelar {vw}: {e}")
        if congeladas:
            cur.execute("notify pgrst, 'reload schema'")
        else:
            print("8. janelas públicas: todas em dia")

        # batimento (o Painel vigia)
        cur.execute("""insert into juridico.robo_status (nome, ultima) values ('rotina7h', now())
                       on conflict (nome) do update set ultima = excluded.ultima""")
        conn.commit()
    print("rotina diária concluída.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
