from fastapi import FastAPI

app = FastAPI(title="Prism")

@app.get("/health")
async def health():
    return {"status": "ok"}