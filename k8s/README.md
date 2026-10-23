# Развёртывание в Kubernetes (on-prem)

Состав: PostgreSQL 16 (StatefulSet), API агента (Deployment, 2 реплики), первичная выгрузка (Job),
ежедневное инкрементальное обновление (CronJob, 03:00 по Алматы).

```bash
# 1. Образ — собрать и положить во внутренний реестр, заменить image в 05–07
docker build -t registry.local/goszakup-agent:1.0 . && docker push registry.local/goszakup-agent:1.0

# 2. Пространство имён, секреты, настройки
kubectl apply -f k8s/00-namespace.yaml
cp k8s/01-secret.example.yaml k8s/secret.yaml   # заполнить значения; secret.yaml в .gitignore
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/02-configmap.yaml

# 3. База: схема создаётся при первом старте из db/init
kubectl -n goszakup create configmap db-init --from-file=db/init
kubectl apply -f k8s/03-postgres.yaml
kubectl -n goszakup rollout status statefulset/postgres

# 4. Данные: первичная выгрузка (несколько часов), затем ежедневные обновления
kubectl apply -f k8s/04-data-pvc.yaml
kubectl apply -f k8s/05-initial-load-job.yaml
kubectl -n goszakup logs -f job/initial-load
kubectl apply -f k8s/06-update-cronjob.yaml

# 5. LLM (необязательно)
kubectl apply -f k8s/08-llm.yaml
kubectl -n goszakup exec deploy/llm -- ollama pull qwen3:4b-instruct-2507-q4_K_M

# 6. Агент
kubectl apply -f k8s/07-agent.yaml
kubectl -n goszakup port-forward svc/agent 8000:8000      # http://localhost:8000/docs
```

Проверка вне расписания: `kubectl -n goszakup create job --from=cronjob/daily-update manual-update`.

Агенту передаётся только пароль роли `agent_ro` (чтение); токена OWS и пароля `etl` у него нет.
