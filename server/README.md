# Mapa de Conexões (Zabbix)

Mapa estilo ViaIpê que lê hosts, itens e problemas do Zabbix e mostra o status de cada ponto.
Só usa Python 3.8+ (biblioteca padrão). O navegador carrega o Leaflet e o OpenStreetMap pela internet.

## Rodar

```bash
# Demonstração (dados simulados)
MOCK=1 python3 server.py        # abra http://localhost:8080

# Com o seu Zabbix
export ZABBIX_TOKEN="seu-token-de-api"
python3 server.py
```

Para o Zabbix real, edite o `config.json`: `"mock": false`, `zabbix.url`
(`https://SEU-ZABBIX/api_jsonrpc.php`) e `groups` (nomes dos grupos de hosts a exibir).
Crie o token em Zabbix > Usuários > Tokens de API, com um usuário **somente leitura**
que enxergue esses grupos.

## O que cada host precisa no Zabbix

| Dado | Onde | Exemplo |
|---|---|---|
| Coordenadas | Inventário do host: `location_lat` / `location_lon` (ou tags `lat` / `lon`) | -5.1234 / -42.8000 |
| Tipo do enlace | Tag `tipo` = `wan`, `core-dist` ou `dist-acesso` (padrão: `wan`) | `tipo=wan` |
| Capacidade | Tag `capacidade_mbps` (sem ela não há % de ocupação) | `capacidade_mbps=100` |
| Interface medida | Tag `interface` (opcional): só considera itens `net.if.*` cuja chave contém esse texto | `interface=Gi0/1` |

O inventário precisa estar habilitado no host (modo manual ou automático).
Hosts sem coordenadas aparecem na lista lateral, mas não no mapa.

## Itens lidos (por prefixo da chave, ajustável em `config.json > metrics`)

- `icmpping` (0 = fora), `icmppingsec` (latência, em segundos, convertida para ms), `icmppingloss` (perda, %)
- `net.if.in` / `net.if.out` (bps). Chaves com error, discard ou dropped são ignoradas.
  Havendo várias interfaces, vale a de maior valor, a menos que a tag `interface` indique qual.
- Se a sua latência já vem em ms, use `"latency_factor": 1`.
- Dado com mais de `stale_minutes` (15) é tratado como sem coleta.

## Regras de status

| Cor | Status | Critério |
|---|---|---|
| Verde | Normal | dentro dos limites |
| Amarelo | Degradado | latência, perda ou ocupação acima do limite de alerta, ou problema ativo de severidade >= 2 |
| Vermelho | Crítico | acima do limite crítico, ou problema ativo de severidade >= 4 |
| Preto | Fora | `icmpping` = 0, ou interface indisponível sem dados |
| Azul | Sem coleta | nenhum dado recente |

Limites por tipo de enlace em `config.json > thresholds`. Os valores são um ponto de partida para calibrar.

## API

- `GET /api/points`: todos os pontos com status e valores atuais (cache de 30 s)
- `GET /api/history?id=<hostid>&hours=24`: séries de latência, perda e tráfego (Mbps). Até 48 h usa histórico, acima disso usa trends.

## Limitações atuais

- Não há mapa de topologia do campus (core, distribuição e acesso): as distribuições entram como pontos
  no mapa geográfico, com coordenadas do prédio.
- Não considera redundância de uplinks nem erros de interface na nota (os itens de erro são ignorados).
- Sem autenticação própria nem exportação CSV/XLSX. Se for publicar, ponha atrás de um proxy (Nginx) com login ou restrito à rede interna.
- Testado em modo demo e contra um Zabbix simulado com as mesmas chamadas da API
  (`hostgroup.get`, `host.get`, `item.get`, `trigger.get`, `history.get`, `trend.get`). Falta rodar contra o seu Zabbix real.
- O ajuste de autenticação depende da versão: o padrão é o cabeçalho `Authorization: Bearer`
  (Zabbix 6.4+/7.x). Se o seu rejeitar, tente `"auth_in_body": true`.
