#!/bin/bash

# OpenRAG Ansible Deployment Script
# Provides interactive and command-line deployment options for OpenRAG

set -e

# -------------------- Configuration Constants --------------------
readonly MAIN_PLAYBOOK="playbooks/main-playbook.yml"
readonly NVIDIA_PLAYBOOK="playbooks/nvidia-drivers-toolkit.yml"
readonly DOCKER_PLAYBOOK="playbooks/docker.yml"
readonly OPENRAG_PLAYBOOK="playbooks/openrag.yml"
readonly INVENTORY_FILE="inventory.ini"

# -------------------- Colors and logging formatting --------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_status() { printf "${GREEN}[INFO]${NC} %s\n" "$1"; }
print_warning() { printf "${YELLOW}[WARNING]${NC} %s\n" "$1"; }
print_error() { printf "${RED}[ERROR]${NC} %s\n" "$1"; }
print_header() { printf "${BLUE}%s${NC}\n" "$1"; }
# -----------------------------------------------------------------------

# -------------------------- Environment Check --------------------------
check_ansible() {
    if ! command -v ansible &> /dev/null; then
        print_error "Ansible is not installed."
        echo -n "Would you like to install Ansible automatically? (y/n): "
        read -r install_choice
        
        if [[ "$install_choice" =~ ^[Yy]$ ]]; then
            print_status "Installing Ansible..."
            
            # Ubuntu/Debian
            if command -v apt &> /dev/null; then
                sudo apt update
                sudo apt install -y ansible
            # Fedora
            elif command -v dnf &> /dev/null; then
                sudo dnf install -y ansible
            # CentOS/RHEL
            elif command -v yum &> /dev/null; then
                sudo yum install -y epel-release
                sudo yum install -y ansible
            # macOS with Homebrew
            elif command -v brew &> /dev/null; then
                brew install ansible
            else
                print_error "Cannot install Ansible automatically. Please install it manually. (Supported: apt, dnf, yum, brew)"
                exit 1
            fi
        else
            print_error "Ansible installation cancelled. Please install Ansible manually and run this script again."
            exit 1
        fi
    else
        print_status "Ansible is already installed: $(ansible --version | head -n1)"
    fi
}

check_prerequisites() {
    print_header "Checking prerequisites..."
    check_ansible
    print_status "Prerequisites check completed"
}
# -----------------------------------------------------------------------


# -------------------------- Local deployment ---------------------------
deploy_local() {
    print_header "Deploying OpenRAG to local machine..."
    
    # Get deployment type from user
    echo ""
    echo "Select deployment type:"
    echo "  1) CPU-only deployment"
    echo "  2) GPU-enabled deployment"
    echo ""
    read -p "Enter your choice (1 or 2): " choice
    
    if [[ "$choice" != "1" && "$choice" != "2" ]]; then
        print_error "Invalid choice. Please select 1 or 2."
        exit 1
    fi
    
    if [ -f "$INVENTORY_FILE" ]; then
        print_warning "Existing $INVENTORY_FILE will be overwritten."
        read -p "Continue? (y/n): " confirm
        if [[ "$confirm" != [Yy] ]]; then
            print_status "Deployment cancelled."
            exit 1
        fi
    fi
    
    if [ "$choice" == "1" ]; then
        print_status "Setting up CPU-only deployment..."
        create_local_cpu_inventory
    else
        print_status "Setting up GPU-enabled deployment..."
        create_local_gpu_inventory
    fi
    
    # Deploy with ansible
    print_status "Starting deployment..."
    ansible-playbook -i "$INVENTORY_FILE" "$MAIN_PLAYBOOK" --ask-become-pass
}

# Helper function to create CPU inventory
create_local_cpu_inventory() {
    cat > "$INVENTORY_FILE" << 'EOF'
[gpu_servers]

[cpu_servers]
localhost ansible_connection=local

[openrag_servers:vars]
ansible_python_interpreter=/usr/bin/python3
EOF
}

# Helper function to create GPU inventory
create_local_gpu_inventory() {
    cat > "$INVENTORY_FILE" << 'EOF'
[gpu_servers]
localhost ansible_connection=local

[cpu_servers]

[openrag_servers:vars]
ansible_python_interpreter=/usr/bin/python3
EOF
}
# -----------------------------------------------------------------------

# -------------------------- Advanced deployment ------------------------
deploy_remote() {
    print_header "Deploying OpenRAG with GPU/CPU server distinction..."
    check_inventory
    ansible-playbook -i "$INVENTORY_FILE" "$MAIN_PLAYBOOK" --ask-become-pass
}

# -------------------------- Modular deployment -------------------------
# Helper function to check inventory
check_inventory() {
    if [ ! -f "$INVENTORY_FILE" ]; then
        print_error "$INVENTORY_FILE not found. Please create it first."
        exit 1
    fi
}

deploy_nvidia_only() {
    print_header "Installing NVIDIA drivers and container toolkit only..."
    check_inventory
    ansible-playbook -i "$INVENTORY_FILE" "$NVIDIA_PLAYBOOK" --ask-become-pass
}

deploy_docker_only() {
    print_header "Installing Docker only..."
    check_inventory
    ansible-playbook -i "$INVENTORY_FILE" "$DOCKER_PLAYBOOK" --ask-become-pass
}

deploy_openrag_only() {
    print_header "Deploying OpenRAG application only..."
    check_inventory
    
    print_warning "This assumes Docker is already installed and running."
    read -p "Continue? (y/n): " confirm
    if [[ "$confirm" != [Yy] ]]; then
        print_status "Deployment cancelled."
        return
    fi
    
    ansible-playbook -i "$INVENTORY_FILE" "$OPENRAG_PLAYBOOK" --ask-become-pass
}


# -----------------------------------------------------------------------

# ------------- Helper functions for deployment management --------------
# Helper function for service operations
run_service_command() {
    local action="$1"
    local command="$2"
    local description="$3"
    
    print_header "$description"
    
    ansible all -i "$INVENTORY_FILE" -m shell -a "$command" --become-user="\$(whoami)"
}

show_playbook_info() {
    print_header "OpenRAG Modular Playbooks Information"
    echo ""
    echo "This deployment uses modular Ansible playbooks located in the 'playbooks/' directory:"
    echo ""
    echo "📋 playbooks/main-playbook.yml - Orchestrates all components"
    echo "   Runs all three playbooks in sequence"
    echo ""
    echo "🔧 playbooks/nvidia-drivers-toolkit.yml - NVIDIA Components"
    echo "   • NVIDIA GPU drivers (version 535)"
    echo "   • NVIDIA Container Toolkit for Docker GPU support"
    echo "   • Target: gpu_servers group"
    echo ""
    echo "🐳 playbooks/docker.yml - Docker Installation"
    echo "   • Docker CE and Docker Compose"
    echo "   • Docker service configuration"
    echo "   • Target: all servers"
    echo ""
    echo "🚀 playbooks/openrag.yml - OpenRAG Application"
    echo "   • Repository cloning and setup"
    echo "   • Environment configuration"
    echo "   • Container deployment with smart profile selection"
    echo "   • Conditional NVIDIA testing (GPU servers only)"
    echo "   • Target: gpu_servers and cpu_servers (unified play)"
    echo ""
    echo "For more detailed information, see README-modular.md"
    echo ""
}

check_status() {
    run_service_command "status" "docker ps --format 'table {{'{{'}}.Names{{'}}'}}\t{{'{{'}}.Status{{'}}'}}'" "Checking OpenRAG deployment status..."
}

stop_services() {
    run_service_command "stop" "if [ -d /home/\$(whoami)/openrag ]; then cd /home/\$(whoami)/openrag && docker compose down; else echo 'OpenRAG directory not found. No services to stop.'; fi" "Stopping OpenRAG services..."
}

start_services() {
    run_service_command "start" "if [ -d /home/\$(whoami)/openrag ]; then cd /home/\$(whoami)/openrag && docker compose up -d; else echo 'OpenRAG directory not found. Please deploy OpenRAG first.'; fi" "Starting OpenRAG services..."
}

show_logs() {
    SERVICE=${1:-openrag}
    run_service_command "logs" "if [ -d /home/\$(whoami)/openrag ]; then cd /home/\$(whoami)/openrag && docker compose logs -f $SERVICE; else echo 'OpenRAG directory not found. Please deploy OpenRAG first.'; fi" "Showing OpenRAG logs..."
}

update_deployment() {
    print_header "Updating OpenRAG deployment..."
    
    echo ""
    echo "Select update method:"
    echo "  1) Update only OpenRAG application (git pull + rebuild)"
    echo "  2) Full system update (re-run entire deployment)"
    echo ""
    read -p "Enter your choice (1 or 2): " update_choice
    
    case $update_choice in
        1)
            print_status "Updating OpenRAG application only..."
            run_service_command "update" "if [ -d /home/\$(whoami)/openrag ]; then cd /home/\$(whoami)/openrag && git pull origin main && docker compose down && docker compose build && docker compose up -d; else echo 'OpenRAG directory not found. Please deploy OpenRAG first using option 1 or 2.'; fi" "Updating OpenRAG application..."
            ;;
        2)
            print_status "Running full system update..."
            check_inventory
            ansible-playbook -i "$INVENTORY_FILE" "$MAIN_PLAYBOOK" --ask-become-pass
            ;;
        *)
            print_error "Invalid choice. Please select 1 or 2."
            ;;
    esac
}

remove_all() {
    print_header "Complete removal of OpenRAG and all dependencies..."
    print_warning "This will remove Docker, NVIDIA toolkit, NVIDIA drivers, and OpenRAG directory!"
    print_warning "This action is IRREVERSIBLE and may damage your system!"
    echo ""
    printf "Are you absolutely sure you want to proceed? Type 'YES' to confirm: "
    read -r confirmation
    
    if [ "$confirmation" != "YES" ]; then
        print_status "Removal cancelled."
        return
    fi
    
    print_status "Starting complete removal process..."
    
    # Stop and remove all Docker containers and images
    print_status "Stopping and removing Docker containers and images..."
    ansible all -i "$INVENTORY_FILE" -m shell -a "
        # Stop all Docker containers
        if command -v docker &> /dev/null; then
            docker stop \$(docker ps -aq) 2>/dev/null || true
            docker rm \$(docker ps -aq) 2>/dev/null || true
            docker system prune -af --volumes 2>/dev/null || true
            docker image prune -af 2>/dev/null || true
        fi
    " --become --ask-become-pass
    
    # Remove OpenRAG directory
    print_status "Removing OpenRAG directory..."
    ansible all -i "$INVENTORY_FILE" -m shell -a "
        if [ -d /home/\$(whoami)/openrag ]; then
            rm -rf /home/\$(whoami)/openrag
            echo 'OpenRAG directory removed'
        else
            echo 'OpenRAG directory not found'
        fi
    " --become-user="\$(whoami)"
    
    # Remove Docker and Docker Compose
    print_status "Removing Docker and Docker Compose..."
    ansible all -i "$INVENTORY_FILE" -m shell -a "
        # Detect package manager and remove Docker accordingly
        if command -v apt &> /dev/null; then
            # Ubuntu/Debian
            apt-get remove -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin docker-compose 2>/dev/null || true
            apt-get purge -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin docker-compose 2>/dev/null || true
            apt-get autoremove -y 2>/dev/null || true
            rm -rf /var/lib/docker /etc/docker /var/lib/containerd
            groupdel docker 2>/dev/null || true
        elif command -v dnf &> /dev/null; then
            # Fedora
            dnf remove -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin docker-compose 2>/dev/null || true
            rm -rf /var/lib/docker /etc/docker /var/lib/containerd
            groupdel docker 2>/dev/null || true
        elif command -v yum &> /dev/null; then
            # CentOS/RHEL
            yum remove -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin docker-compose 2>/dev/null || true
            rm -rf /var/lib/docker /etc/docker /var/lib/containerd
            groupdel docker 2>/dev/null || true
        else
            echo 'Unknown package manager. Please remove Docker manually.'
        fi
        echo 'Docker removal completed'
    " --become --ask-become-pass
    
    # Remove NVIDIA Container Toolkit
    print_status "Removing NVIDIA Container Toolkit..."
    ansible all -i "$INVENTORY_FILE" -m shell -a "
        if command -v apt &> /dev/null; then
            # Ubuntu/Debian
            apt-get remove -y nvidia-container-toolkit nvidia-container-runtime nvidia-docker2 2>/dev/null || true
            apt-get purge -y nvidia-container-toolkit nvidia-container-runtime nvidia-docker2 2>/dev/null || true
        elif command -v dnf &> /dev/null; then
            # Fedora
            dnf remove -y nvidia-container-toolkit nvidia-container-runtime nvidia-docker2 2>/dev/null || true
        elif command -v yum &> /dev/null; then
            # CentOS/RHEL
            yum remove -y nvidia-container-toolkit nvidia-container-runtime nvidia-docker2 2>/dev/null || true
        fi
        echo 'NVIDIA Container Toolkit removal completed'
    " --become --ask-become-pass
    
    # Remove NVIDIA drivers
    print_status "Removing NVIDIA drivers..."
    ansible all -i "$INVENTORY_FILE" -m shell -a "
        if command -v nvidia-uninstall &> /dev/null; then
            # If NVIDIA installer was used
            nvidia-uninstall --silent 2>/dev/null || true
        fi
        
        if command -v apt &> /dev/null; then
            # Ubuntu/Debian
            apt-get remove -y nvidia-* libnvidia-* 2>/dev/null || true
            apt-get purge -y nvidia-* libnvidia-* 2>/dev/null || true
            apt-get autoremove -y 2>/dev/null || true
        elif command -v dnf &> /dev/null; then
            # Fedora
            dnf remove -y nvidia-* akmod-nvidia xorg-x11-drv-nvidia* 2>/dev/null || true
        elif command -v yum &> /dev/null; then
            # CentOS/RHEL
            yum remove -y nvidia-* kmod-nvidia 2>/dev/null || true
        fi
        
        # Remove NVIDIA configuration files
        rm -rf /etc/X11/xorg.conf.d/*nvidia* 2>/dev/null || true
        rm -rf /usr/share/X11/xorg.conf.d/*nvidia* 2>/dev/null || true
        
        echo 'NVIDIA drivers removal completed'
    " --become --ask-become-pass
    
    # Clean up package caches and repositories
    print_status "Cleaning up package caches and repositories..."
    ansible all -i "$INVENTORY_FILE" -m shell -a "
        if command -v apt &> /dev/null; then
            # Remove NVIDIA and Docker repositories
            rm -f /etc/apt/sources.list.d/nvidia-container-toolkit.list 2>/dev/null || true
            rm -f /etc/apt/sources.list.d/docker.list 2>/dev/null || true
            rm -f /etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg 2>/dev/null || true
            rm -f /etc/apt/keyrings/docker.gpg 2>/dev/null || true
            apt-get update 2>/dev/null || true
            apt-get autoremove -y 2>/dev/null || true
            apt-get autoclean 2>/dev/null || true
        elif command -v dnf &> /dev/null; then
            # Fedora
            rm -f /etc/yum.repos.d/nvidia-container-toolkit.repo 2>/dev/null || true
            rm -f /etc/yum.repos.d/docker-ce.repo 2>/dev/null || true
            dnf clean all 2>/dev/null || true
        elif command -v yum &> /dev/null; then
            # CentOS/RHEL
            rm -f /etc/yum.repos.d/nvidia-container-toolkit.repo 2>/dev/null || true
            rm -f /etc/yum.repos.d/docker-ce.repo 2>/dev/null || true
            yum clean all 2>/dev/null || true
        fi
        echo 'Cleanup completed'
    " --become --ask-become-pass
    
    print_status "Complete removal finished!"
    print_warning "You may need to reboot the system to complete the NVIDIA driver removal."
    print_warning "Please verify that all components have been removed successfully."
}
# -----------------------------------------------------------------------

# ------------------------------ Main Menu ------------------------------
show_menu() {
    print_header "OpenRAG Ansible Deployment Tool"
    echo "=== Full Deployment ==="
    echo "1) Deploy to local machine (full installation)"
    echo "2) Deploy remotely (full installation)"
    echo ""
    echo "=== Modular Deployment ==="
    echo "3) Install NVIDIA drivers and toolkit only"
    echo "4) Install Docker only"
    echo "5) Deploy OpenRAG application only"
    echo ""
    echo "=== Management ==="
    echo "6) Check deployment status"
    echo "7) Stop services"
    echo "8) Start services"
    echo "9) Show logs"
    echo "10) Update deployment"
    echo "11) Remove all (OpenRAG, Docker, NVIDIA drivers)"
    echo ""
    echo "=== Information ==="
    echo "12) Show playbook information"
    echo "13) Exit"
    echo ""
    read -p "Choose an option [1-13]: " choice
}

main() {
    cd "$(dirname "$0")"
    check_prerequisites
    
    if [ $# -eq 0 ]; then
        while true; do
            show_menu
            case $choice in
                1)
                    deploy_local
                    ;;
                2)
                    deploy_remote
                    ;;
                3)
                    deploy_nvidia_only
                    ;;
                4)
                    deploy_docker_only
                    ;;
                5)
                    deploy_openrag_only
                    ;;
                6)
                    check_status
                    ;;
                7)
                    stop_services
                    ;;
                8)
                    start_services
                    ;;
                9)
                    echo "Enter service name (default: openrag):"
                    read service_name
                    show_logs "$service_name"
                    ;;
                10)
                    update_deployment
                    ;;
                11)
                    remove_all
                    ;;
                12)
                    show_playbook_info
                    ;;
                13)
                    print_status "Goodbye!"
                    exit 0
                    ;;
                *)
                    print_error "Invalid option. Please choose 1-13."
                    ;;
            esac
            echo ""
            read -p "Press Enter to continue..."
        done
    else
        case $1 in
            "deploy-local")
                deploy_local
                ;;
            "deploy-remote")
                deploy_remote
                ;;
            "deploy-nvidia")
                deploy_nvidia_only
                ;;
            "deploy-docker")
                deploy_docker_only
                ;;
            "deploy-openrag")
                deploy_openrag_only
                ;;
            "status")
                check_status
                ;;
            "stop")
                stop_services
                ;;
            "start")
                start_services
                ;;
            "logs")
                show_logs "$2"
                ;;
            "update")
                update_deployment
                ;;
            "remove-all")
                remove_all
                ;;
            "info")
                show_playbook_info
                ;;
            *)
                echo "Usage: $0 [deploy-local|deploy-remote|deploy-nvidia|deploy-docker|deploy-openrag|status|stop|start|logs [service]|update|remove-all|info]"
                exit 1
                ;;
        esac
    fi
}

main "$@"
# -----------------------------------------------------------------------
