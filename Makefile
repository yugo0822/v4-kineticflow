# Variable definitions
IMAGE_NAME = v4-dashboard
CONTAINER_NAME = v4-mppi-run

# ============================================
# Optional .env loading (do NOT commit .env)
# ============================================
#
# This allows:
#   - storing PRIVATE_KEY / BASE_SEPOLIA_RPC_URL in .env
#   - running `make deploy-base-sepolia` without inline secrets
#
# .env format must be simple KEY=VALUE lines (compatible with Make).
ifneq ($(wildcard .env),)
  include .env
  export
endif

# Deployment settings
ANVIL_RPC_URL ?= http://127.0.0.1:8545
BASE_SEPOLIA_RPC_URL ?= https://sepolia.base.org
ANVIL_PRIVATE_KEY ?= 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
SCRIPT_DIR = script

.PHONY: build run stop shell logs re
.PHONY: anvil-start anvil-stop deploy-anvil deploy-base-sepolia deploy-all clean test

# ============================================
# Docker Commands
# ============================================

# Build Docker image
build:
	docker build -t $(IMAGE_NAME) .

# Run Docker container
# --add-host host.docker.internal:host-gateway is required for Linux environments to use host.docker.internal (not needed for Mac Docker Desktop, but included for safety)
run:
	docker run -d \
		--name $(CONTAINER_NAME) \
		--add-host host.docker.internal:host-gateway \
		-p 8501:8501 \
		-v $(PWD)/broadcast:/app/broadcast \
		-e ANVIL_RPC_URL="http://host.docker.internal:8545" \
		$(IMAGE_NAME)
	@echo "---------------------------------------------------"
	@echo "Dashboard is accessible at: http://localhost:8501"
	@echo "---------------------------------------------------"

# Stop and remove container
stop:
	docker stop $(CONTAINER_NAME) || true
	docker rm $(CONTAINER_NAME) || true

# View Docker logs
logs:
	docker logs -f $(CONTAINER_NAME)

# Enter Docker container shell
shell:
	docker exec -it $(CONTAINER_NAME) /bin/bash

# Rebuild and restart container
re: stop build run 

# ============================================
# Anvil Commands
# ============================================

# Start Anvil in background
anvil-start:
	@echo "Starting Anvil..."
	@if pgrep -f "anvil" > /dev/null; then \
		echo "Anvil is already running"; \
	else \
		anvil > anvil.log 2>&1 & \
		echo "Anvil started in background (PID: $$!)"; \
		echo "Logs: tail -f anvil.log"; \
	fi

# Stop Anvil
anvil-stop:
	@echo "Stopping Anvil..."
	@pkill -f "anvil" || true
	@echo "Anvil stopped"

# Check Anvil status
anvil-status:
	@if pgrep -f "anvil" > /dev/null; then \
		echo "Anvil is running (PID: $$(pgrep -f anvil))"; \
		cast block-number --rpc-url $(ANVIL_RPC_URL) 2>/dev/null && echo "Connected to Anvil" || echo "Cannot connect to Anvil"; \
	else \
		echo "Anvil is not running"; \
	fi

# ============================================
# Deploy Commands
# ============================================

# Deploy to local Anvil (runs all scripts)
deploy-anvil: anvil-status
	@echo "=========================================="
	@echo "Deploying to Anvil (Local)"
	@echo "=========================================="
	@echo "RPC URL: $(ANVIL_RPC_URL)"
	@echo "Private Key: $(ANVIL_PRIVATE_KEY)"
	@echo ""
	forge script $(SCRIPT_DIR)/AnvilRun.s.sol \
		--rpc-url $(ANVIL_RPC_URL) \
		--broadcast \
		--private-key $(ANVIL_PRIVATE_KEY)
	@echo ""
	@echo "=========================================="
	@echo "Deployment completed!"
	@echo "Addresses saved to: broadcast/addresses.json"
	@echo "=========================================="

# Deploy to Base Sepolia
deploy-base-sepolia:
	@if [ -z "$$PRIVATE_KEY" ]; then \
		echo "Error: PRIVATE_KEY environment variable is required"; \
		echo "Usage: PRIVATE_KEY=0x... BASE_SEPOLIA_RPC_URL=... make deploy-base-sepolia"; \
		echo "Tip: put PRIVATE_KEY and BASE_SEPOLIA_RPC_URL into .env (not committed) and re-run"; \
		exit 1; \
	fi
	@echo "=========================================="
	@echo "Deploying to Base Sepolia"
	@echo "=========================================="
	@echo "RPC URL: $(BASE_SEPOLIA_RPC_URL)"
	@echo ""
	forge script $(SCRIPT_DIR)/BaseSepoliaRun.s.sol \
		--rpc-url $(BASE_SEPOLIA_RPC_URL) \
		--broadcast \
		--private-key $$PRIVATE_KEY \
		--verify
	@echo ""
	@echo "=========================================="
	@echo "Deployment completed!"
	@echo "=========================================="

# Individual deployment scripts (optional)
deploy-hook:
	@echo "Deploying Hook only..."
	forge script $(SCRIPT_DIR)/00_DeployHook.s.sol \
		--rpc-url $(ANVIL_RPC_URL) \
		--broadcast \
		--private-key $(ANVIL_PRIVATE_KEY)

deploy-pool:
	@echo "Creating Pool and Adding Liquidity..."
	forge script $(SCRIPT_DIR)/01_CreatePoolAndAddLiquidity.s.sol \
		--rpc-url $(ANVIL_RPC_URL) \
		--broadcast \
		--private-key $(ANVIL_PRIVATE_KEY)

# Full deployment (start Anvil + deploy)
deploy-all: anvil-start
	@sleep 2
	@$(MAKE) deploy-anvil

# ============================================
# Test and Cleanup Commands
# ============================================

# Run tests
test:
	@echo "Running tests..."
	forge test -vvv

# Clean build artifacts and cache
clean:
	@echo "Cleaning up..."
	forge clean
	rm -rf broadcast/*.json cache/*.json out/
	@echo "Cleanup completed"

# Full cleanup (cache, build artifacts, logs)
clean-all: clean
	rm -f anvil.log
	@echo "Full cleanup completed"

# ============================================
# Integrated
# ============================================

start-all: deploy-all re

stop-all: stop anvil-stop


# ============================================
# help
# ============================================

help:
	@echo "Available commands:"
	@echo ""
	@echo "Docker Commands:"
	@echo "  make build          - Build Docker image"
	@echo "  make run            - Run Docker container"
	@echo "  make stop           - Stop Docker container"
	@echo "  make logs           - View Docker logs"
	@echo "  make shell          - Enter Docker container"
	@echo "  make re             - Rebuild and restart container"
	@echo ""
	@echo "Anvil Commands:"
	@echo "  make anvil-start    - Start Anvil in background"
	@echo "  make anvil-stop     - Stop Anvil"
	@echo "  make anvil-status   - Check Anvil status"
	@echo ""
	@echo "Deploy Commands:"
	@echo "  make deploy-anvil    - Deploy to local Anvil"
	@echo "  make deploy-all     - Start Anvil and deploy"
	@echo "  make deploy-base-sepolia - Deploy to Base Sepolia (requires PRIVATE_KEY)"
	@echo "  make deploy-hook    - Deploy Hook only"
	@echo "  make deploy-pool    - Create Pool and add liquidity"
	@echo ""
	@echo "Utility Commands:"
	@echo "  make test           - Run tests"
	@echo "  make clean          - Clean build artifacts"
	@echo "  make clean-all      - Full cleanup"
	@echo "  make help           - Show this help"
