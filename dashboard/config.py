import os
import json

def _project_root():
    """Project root (parent of dashboard/)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_contract_addresses():
    """
    Load latest addresses produced by deployment scripts.
    Priority: Base Sepolia (chainid file) > Base Sepolia (explicit) > Anvil.
    """
    root = _project_root()
    # Base Sepolia (chainid-based filename: e.g., addresses.84532.json)
    paths_to_try = [
        "/app/broadcast/addresses.84532.json",          # Docker (absolute)
        os.path.join(root, "broadcast", "addresses.84532.json"),
        "broadcast/addresses.84532.json",
        os.path.join(root, "broadcast", "addresses.json"),
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
