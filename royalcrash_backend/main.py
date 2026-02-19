"""
RoyalCrash Backend — FastAPI + WebSocket
Реальная краш-игра в реальном времени
"""
import asyncio
import hashlib
import hmac
import json
import os
import random
import time
from typing import Dict, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="RoyalCrash Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── СОСТОЯНИЕ ИГРЫ ──────────────────────────────────────────────────────────

class GameState:
    def __init__(self):
        self.phase = "waiting"       # waiting → betting → running → crashed
        self.multiplier = 1.0
        self.crash_point = 1.0
        self.round_id = 0
        self.start_time = 0.0
        self.bets: Dict[int, dict] = {}       # user_id → {amount, auto_cashout, cashed_out}
        self.history: list = []               # последние 20 раундов
        self.connections: Set[WebSocket] = set()

game = GameState()

# ── ГЕНЕРАЦИЯ КРАШ-ТОЧКИ (честная) ──────────────────────────────────────────

def generate_crash_point(seed: str) -> float:
    """Честный краш через HMAC-SHA256"""
    h = hmac.new(b"royalcrash_secret_key", seed.encode(), hashlib.sha256).hexdigest()
    val = int(h[:8], 16)
    # Дом имеет 3% преимущество
    if val % 33 == 0:
        return 1.0
    result = (100 / (1 - (val / 0xFFFFFFFF))) / 100
    return round(min(result, 10000.0), 2)

# ── WEBSOCKET МЕНЕДЖЕР ───────────────────────────────────────────────────────

async def broadcast(message: dict):
    """Разослать сообщение всем подключённым клиентам"""
    if not game.connections:
        return
    data = json.dumps(message)
    dead = set()
    for ws in game.connections.copy():
        try:
            await ws.send_text(data)
        except Exception:
            dead.add(ws)
    game.connections -= dead

# ── ИГРОВОЙ ЦИКЛ ────────────────────────────────────────────────────────────

async def game_loop():
    """Основной игровой цикл"""
    while True:
        # ── ФАЗА СТАВОК (7 секунд) ──
        game.phase = "betting"
        game.round_id += 1
        game.bets = {}
        seed = f"{game.round_id}_{time.time()}_{random.random()}"
        game.crash_point = generate_crash_point(seed)

        await broadcast({
            "type": "betting_start",
            "round_id": game.round_id,
            "duration": 7000
        })
        await asyncio.sleep(7)

        # ── ФАЗА ИГРЫ ──
        game.phase = "running"
        game.multiplier = 1.0
        game.start_time = time.time()

        await broadcast({
            "type": "round_start",
            "round_id": game.round_id,
            "bets": [
                {"user_id": uid, "amount": b["amount"], "username": b.get("username", "?")}
                for uid, b in game.bets.items()
            ]
        })

        # Растим множитель
        tick = 0.1  # секунд между тиками
        while game.multiplier < game.crash_point:
            await asyncio.sleep(tick)
            elapsed = time.time() - game.start_time
            # Формула роста: e^(0.00006 * ms)
            game.multiplier = round(pow(1.0024, elapsed * 10), 2)

            if game.multiplier >= game.crash_point:
                break

            # Авто-кешаут
            cashouts = []
            for uid, bet in game.bets.items():
                if not bet["cashed_out"] and bet["auto_cashout"] and game.multiplier >= bet["auto_cashout"]:
                    bet["cashed_out"] = True
                    bet["cashout_at"] = game.multiplier
                    cashouts.append({"user_id": uid, "multiplier": game.multiplier, "username": bet.get("username", "?")})

            await broadcast({
                "type": "tick",
                "multiplier": game.multiplier,
                "cashouts": cashouts
            })

        # ── КРАШ ──
        game.phase = "crashed"
        game.multiplier = game.crash_point

        # Считаем результаты
        results = []
        for uid, bet in game.bets.items():
            won = bet["cashed_out"]
            results.append({
                "user_id": uid,
                "username": bet.get("username", "?"),
                "amount": bet["amount"],
                "cashed_out": won,
                "cashout_at": bet.get("cashout_at", None)
            })

        # Добавляем в историю
        game.history.insert(0, {
            "round_id": game.round_id,
            "crash_point": game.crash_point,
            "timestamp": int(time.time())
        })
        game.history = game.history[:20]

        await broadcast({
            "type": "crashed",
            "multiplier": game.crash_point,
            "round_id": game.round_id,
            "results": results,
            "history": game.history[:7]
        })

        await asyncio.sleep(3)

# ── WEBSOCKET ENDPOINT ───────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    game.connections.add(websocket)

    # Отправляем текущее состояние
    await websocket.send_text(json.dumps({
        "type": "state",
        "phase": game.phase,
        "multiplier": game.multiplier,
        "round_id": game.round_id,
        "history": game.history[:7],
        "players_online": len(game.connections)
    }))

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)

            if msg["type"] == "place_bet":
                user_id = msg.get("user_id")
                amount = float(msg.get("amount", 0))
                auto_cashout = msg.get("auto_cashout")
                username = msg.get("username", "Игрок")

                if game.phase != "betting":
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Ставки принимаются только до начала раунда"
                    }))
                    continue

                if amount <= 0:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "Некорректная сумма ставки"
                    }))
                    continue

                game.bets[user_id] = {
                    "amount": amount,
                    "auto_cashout": float(auto_cashout) if auto_cashout else None,
                    "cashed_out": False,
                    "cashout_at": None,
                    "username": username
                }

                await broadcast({
                    "type": "bet_placed",
                    "user_id": user_id,
                    "username": username,
                    "amount": amount
                })

            elif msg["type"] == "cashout":
                user_id = msg.get("user_id")
                if game.phase == "running" and user_id in game.bets:
                    bet = game.bets[user_id]
                    if not bet["cashed_out"]:
                        bet["cashed_out"] = True
                        bet["cashout_at"] = game.multiplier
                        winnings = round(bet["amount"] * game.multiplier, 4)

                        await broadcast({
                            "type": "cashout",
                            "user_id": user_id,
                            "username": bet.get("username", "?"),
                            "multiplier": game.multiplier,
                            "winnings": winnings
                        })

    except WebSocketDisconnect:
        game.connections.discard(websocket)
    except Exception:
        game.connections.discard(websocket)

# ── REST API ─────────────────────────────────────────────────────────────────

@app.get("/state")
async def get_state():
    return {
        "phase": game.phase,
        "multiplier": game.multiplier,
        "round_id": game.round_id,
        "players_online": len(game.connections),
        "history": game.history[:7]
    }

@app.get("/history")
async def get_history():
    return {"history": game.history}

@app.get("/health")
async def health():
    return {"status": "ok", "round_id": game.round_id}

# ── STARTUP ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    asyncio.create_task(game_loop())
    print("🔱 RoyalCrash Backend started!")
