import asyncio
import aiohttp
import time

async def send_transaction(session, from_address, to_address, amount):
    url = "http://127.0.0.1:5000/add_transaction"
    data = {
        "from_address": from_address,
        "to_address": to_address,
        "amount": amount
    }
    async with session.post(url, json=data) as response:
        return await response.json()

async def run_transaction_batch(session, round_index, num_transactions):
    tasks = [
        send_transaction(session, f"{i}", f"{i+TRANSACTIONS_PER_ROUND}", 1)
        for i in range(num_transactions)
    ]

    start_time = time.perf_counter()
    results = await asyncio.gather(*tasks)
    end_time = time.perf_counter()

    duration = end_time - start_time
    print(f"[Round {round_index + 1}] {num_transactions} transactions envoyées en {duration:.2f} secondes")

    return duration, results

async def main():
    total_durations = []

    async with aiohttp.ClientSession() as session:
        for round_index in range(NUM_ROUNDS):
            duration, results = await run_transaction_batch(session, round_index, TRANSACTIONS_PER_ROUND)
            total_durations.append(duration)

    total_tx = NUM_ROUNDS * TRANSACTIONS_PER_ROUND
    total_time = sum(total_durations)
    avg_time = total_time / NUM_ROUNDS
    max_time = max(total_durations)
    min_time = min(total_durations)
    tps = total_tx / total_time if total_time > 0 else 0

    print("\n===== Résumé global =====")
    print(f"Total transactions     : {total_tx}")
    print(f"Total time             : {total_time:.2f} s")
    print(f"Temps moyen par round  : {avg_time:.2f} s")
    print(f"Min / Max round time   : {min_time:.2f} s / {max_time:.2f} s")
    print(f"Transactions par seconde (TPS) : {tps:.2f}")

NUM_ROUNDS = 1
TRANSACTIONS_PER_ROUND = 64

if __name__ == "__main__":
    asyncio.run(main())
