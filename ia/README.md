# IA TensorFlow Lite

Esta pasta inicia a migracao da camada de IA para um fluxo separado em TensorFlow Lite, sem mexer no motor classico do bot.

## Objetivo

- gerar datasets supervisionados a partir do historico do projeto
- treinar uma rede neural pequena para classificar `short`, `hold` e `long`
- exportar o modelo final em `.tflite`
- manter a inferencia leve e separada do runtime principal

## Estrutura

- `dataset_builder.py`: monta features e labels a partir dos candles
- `train_tflite_model.py`: treina um modelo denso pequeno e exporta `.tflite`
- `tflite_inference.py`: carrega o modelo exportado e roda inferencia no ultimo candle
- `requirements-tflite.txt`: dependencias da venv da IA

## Importante sobre Python

O workspace atual roda em Python `3.14`, mas as wheels oficiais do TensorFlow com `pip` sao documentadas para Python `3.9` a `3.12`.

Referencias oficiais:

- https://www.tensorflow.org/install
- https://www.tensorflow.org/install/pip

Por isso, a recomendacao pratica para esta pasta e usar uma venv separada em Python `3.11` ou `3.12`.

## Setup sugerido no Windows

```powershell
py -3.11 -m venv .venv-ai
.venv-ai\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r ia\requirements-tflite.txt
```

## Exemplo: gerar dataset do XLM 15m

```powershell
python ia\dataset_builder.py `
  --symbol XLM/USDT `
  --timeframe 15m `
  --total-limit 30000 `
  --output ia\artifacts\xlm_15m_dataset.npz
```

## Exemplo: treinar e exportar para TFLite

```powershell
python ia\train_tflite_model.py `
  --dataset ia\artifacts\xlm_15m_dataset.npz `
  --output-dir ia\artifacts\xlm_15m_model `
  --epochs 24 `
  --batch-size 64
```

Arquivos gerados:

- `model.keras`
- `model.tflite`
- `metadata.json`
- `history.json`

## Exemplo: inferencia no candle mais recente

```powershell
python ia\tflite_inference.py `
  --model ia\artifacts\xlm_15m_model\model.tflite `
  --metadata ia\artifacts\xlm_15m_model\metadata.json `
  --symbol XLM/USDT `
  --timeframe 15m
```

## Retreino autonomo com gate de seguranca

O script `auto_retrain.py` executa o ciclo completo localmente:

- gera dataset supervisionado dos simbolos aprovados
- treina um candidato `.tflite`
- compara o candidato contra o modelo runtime atual
- salva um relatorio JSON para auditoria/dashboard
- so promove o candidato se passar no gate de qualidade

Exemplo de teste local sem promover:

```powershell
.venv\Scripts\python.exe ia\auto_retrain.py `
  --symbols BTC/USDT `
  --timeframe 15m `
  --total-limit 1500 `
  --label-mode trade_outcome `
  --epochs 2 `
  --output-root ia\artifacts\auto_retrain_test
```

O gate nao olha apenas a acuracia geral. Ele tambem valida:

- `val_loss` maximo
- diferenca minima contra o modelo runtime atual
- quantidade minima de sinais reais na validacao
- precisao minima quando o candidato prevê `short` ou `long`

Esse ultimo ponto evita promover um modelo que acerta muitos candles `hold`, mas gera falsos sinais demais para operar dinheiro real.

Exemplo de promocao local, somente se o gate aprovar:

```powershell
.venv\Scripts\python.exe ia\auto_retrain.py `
  --symbols BTC/USDT,XLM/USDT `
  --timeframe 15m `
  --total-limit 30000 `
  --label-mode trade_outcome `
  --epochs 24 `
  --promote
```

Arquivos principais:

- `latest_auto_retrain_report.json`: ultimo relatorio para dashboard/auditoria
- `candidate_model/model.tflite`: modelo candidato
- `candidate_model/metadata.json`: metricas e matriz de confusao do candidato

## Proximo passo natural

Depois desta base pronta, o ideal e:

1. gerar datasets separados por simbolo e timeframe aprovados
2. comparar a rede neural com o motor classico
3. decidir se a IA vai virar filtro de confirmacao ou motor principal
