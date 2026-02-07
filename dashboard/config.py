import os
import json

def load_contract_addresses():
    """
    Load latest addresses produced by deployment scripts.
    Priority: Base Sepolia (chainid file) > Base Sepolia (explicit) > Anvil.
    """
    # Base Sepolia (chainid-based filename: e.g., addresses.84532.json)
    paths_to_try = [
        # Base Sepolia (chainid-based filename: e.g., addresses.84532.json)
        "/app/broadcast/addresses.84532.json",          # Docker path
        "broadcast/addresses.84532.json",               # Host path
        # Anvil defaults
        "/app/broadcast/addresses.json",
        "broadcast/addresses.json",
    ]
    
    for path in paths_to_try:
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    config = json.load(f)
                # Config loaded silently
                return config
            except Exception as e:
                print(f"Config: Error loading {path}: {e}. Trying next...", flush=True)
                continue
    
    print(f"Config: No addresses file found. Using defaults.", flush=True)

# Load as global configuration
CONTRACTS = load_contract_addresses()
