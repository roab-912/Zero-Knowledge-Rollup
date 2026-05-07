import os
from dotenv import load_dotenv
import time
import classes.ZKRollup as ZKRollup

load_dotenv()

INITIAL_BALANCE = float(os.getenv("INITIAL_BALANCE"))
NB_INITIAL_WALLET = int(os.getenv("NB_INITIAL_WALLET"))
EPOCH = int(os.getenv("EPOCH"))

initial_balances = {str(i): INITIAL_BALANCE for i in range(NB_INITIAL_WALLET)}

zk = ZKRollup.ZKRollup(initial_balances=initial_balances, duration=15, epoch=EPOCH, mode="snarkjs")
zk.start_batch_loop()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("Arrêt du ZKRollup en cours...")
    zk.stop()