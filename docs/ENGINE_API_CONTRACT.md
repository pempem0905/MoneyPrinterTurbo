# Engine API Contract — Initial Internal Use

For the first integration phase, our app backend may call the upstream MoneyPrinterTurbo endpoints directly through the engine service.

Required request header for protected endpoints:

```text
x-api-key: <MPT_API_KEY>
```

Initial endpoints used by our app backend:

- `GET /ping` — container/service health only.
- `POST /api/v1/videos` — enqueue full video generation.
- `GET /api/v1/tasks/{task_id}` — poll generation state and retrieve result metadata.
- `DELETE /api/v1/tasks/{task_id}` — cleanup only when the task is no longer busy.
- `GET /tasks/...` — retrieve generated artifacts with the same API key.

The browser/PWA must not call the engine directly. Our application backend owns the engine credential and translates app-level jobs into MoneyPrinterTurbo requests.

A stable `/engine/*` facade can be added later if upstream request/response changes become costly to absorb in the app backend.
