import sys
import subprocess
import time
import os
import glob
import webbrowser
from botocore.exceptions import ClientError


# ─────────────────────────────────────────────
#   STEP 0 : Auto-install missing dependencies
# ─────────────────────────────────────────────

REQUIRED = {"boto3": "boto3", "paramiko": "paramiko"}

def ensure_dependencies():
    missing = []
    for module, package in REQUIRED.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        print("\n[Setup] Missing packages detected:", ", ".join(missing))
        print("[Setup] Installing automatically...\n")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + missing
        )
        print("[Setup] Done. Restarting script...\n")
        os.execv(sys.executable, [sys.executable] + sys.argv)

ensure_dependencies()

import boto3
import paramiko


# ─────────────────────────────────────────────
#   HELPERS
# ─────────────────────────────────────────────

def header(title):
    width = 44
    print("\n" + "=" * width)
    print(f"  {title}")
    print("=" * width + "\n")


def step(n, total, label):
    print(f"\n[{n}/{total}] {label}...")


def confirm(prompt="Continue?", default="y"):
    hint = "[Y/n]" if default.lower() == "y" else "[y/N]"
    answer = input(f"\n{prompt} {hint}: ").strip().lower()
    if answer == "":
        return default.lower() == "y"
    return answer == "y"


# ─────────────────────────────────────────────
#   AWS CONNECTIVITY CHECK
# ─────────────────────────────────────────────

def check_aws():
    print("[Check] Verifying AWS credentials...")
    try:
        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        print(f"[Check] Connected as: {identity['Arn']}")
        return True
    except Exception:
        print("\n[Error] AWS not configured or credentials invalid.")
        print("        Run:  aws configure\n")
        return False


# ─────────────────────────────────────────────
#   PEM FILE DETECTION
# ─────────────────────────────────────────────

def find_pem_for_instance(key_pair_name):
    """Try to auto-locate a .pem file matching the instance's key pair name."""
    search_dirs = [
        ".",
        os.path.expanduser("~"),
        os.path.expanduser("~/.ssh"),
        os.path.expanduser("~/Downloads"),
        os.path.expanduser("~/Desktop"),
        os.path.expanduser("~/Documents"),
    ]
    for directory in search_dirs:
        candidate = os.path.join(directory, f"{key_pair_name}.pem")
        if os.path.isfile(candidate):
            return candidate
    return None


def find_all_pem_files():
    """Find all .pem files in common locations."""
    search_patterns = [
        "./*.pem",
        os.path.expanduser("~/*.pem"),
        os.path.expanduser("~/.ssh/*.pem"),
        os.path.expanduser("~/Downloads/*.pem"),
        os.path.expanduser("~/Desktop/*.pem"),
        os.path.expanduser("~/Documents/*.pem"),
    ]
    found = []
    seen = set()
    for pattern in search_patterns:
        for path in glob.glob(pattern):
            abs_path = os.path.abspath(path)
            if abs_path not in seen:
                found.append(abs_path)
                seen.add(abs_path)
    return found


def select_pem_file(key_pair_name):
    """Auto-detect PEM or show fallback menu."""

    # 1. Try auto-detect by key pair name
    auto = find_pem_for_instance(key_pair_name)
    if auto:
        print(f"[PEM]  Auto-detected: {auto}")
        return auto

    print(f"[PEM]  Could not auto-detect '{key_pair_name}.pem'")

    # 2. Show all available .pem files
    all_pems = find_all_pem_files()

    if not all_pems:
        print("[PEM]  No .pem files found on this machine.")
        manual = input("       Enter full path to your .pem file: ").strip()
        return manual if os.path.isfile(manual) else None

    print("\nAvailable PEM Files:\n")
    for i, pem in enumerate(all_pems, start=1):
        print(f"  {i}. {os.path.basename(pem)}")
        print(f"     {pem}\n")

    choice = input("Select PEM Number (or press Enter to type path): ").strip()

    if choice == "":
        manual = input("Enter full path to your .pem file: ").strip()
        return manual if os.path.isfile(manual) else None

    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(all_pems):
            return all_pems[idx]

    return None


# ─────────────────────────────────────────────
#   DETECT SSH USER FROM AMI
# ─────────────────────────────────────────────

def detect_ssh_user(instance):
    """
    Detect correct SSH user from AMI name.
    Falls back to ec2-user for Amazon Linux.
    """
    image_id = instance.get("image_id", "")

    try:
        ec2 = boto3.client("ec2")
        ami_info = ec2.describe_images(ImageIds=[image_id])
        ami_name = ami_info["Images"][0].get("Name", "").lower() if ami_info["Images"] else ""
    except Exception:
        ami_name = ""

    if "ubuntu" in ami_name:
        return "ubuntu"
    if "debian" in ami_name:
        # Most official Debian AMIs use 'admin'; some older ones use 'debian'
        # 'admin' is correct for all Debian 10+ official AMIs
        return "admin"
    if "centos" in ami_name:
        return "centos"
    if "fedora" in ami_name:
        return "fedora"
    if "rhel" in ami_name or "red hat" in ami_name:
        return "ec2-user"
    if "suse" in ami_name or "sles" in ami_name:
        return "ec2-user"

    # Default: Amazon Linux / Amazon Linux 2 / Amazon Linux 2023
    return "ec2-user"


# ─────────────────────────────────────────────
#   EC2 INSTANCE LISTING & SELECTION
# ─────────────────────────────────────────────

def get_running_instances():
    ec2 = boto3.client("ec2")
    response = ec2.describe_instances()

    instances = []
    skipped_windows = 0

    for reservation in response["Reservations"]:
        for inst in reservation["Instances"]:
            if inst["State"]["Name"] != "running":
                continue

            # Skip Windows — SSH not supported
            if inst.get("Platform", "").lower() == "windows":
                skipped_windows += 1
                continue

            name = "No Name"
            for tag in inst.get("Tags", []):
                if tag["Key"] == "Name":
                    name = tag["Value"]

            instances.append({
                "id":       inst["InstanceId"],
                "name":     name,
                "ip":       inst.get("PublicIpAddress", "N/A"),
                "key_pair": inst.get("KeyName", "Unknown"),
                "image_id": inst.get("ImageId", ""),
                "platform": inst.get("Platform", ""),
            })

    if skipped_windows:
        print(f"[Info]  Skipped {skipped_windows} Windows instance(s) — SSH not supported.")

    return instances


def select_instance():
    instances = get_running_instances()

    if not instances:
        print("\nNo running Linux instances found.")
        return None

    print("Running Instances:\n")
    for i, inst in enumerate(instances, start=1):
        print(f"  {i}. {inst['name']}")
        print(f"     Instance ID : {inst['id']}")
        print(f"     Public IP   : {inst['ip']}")
        print(f"     Key Pair    : {inst['key_pair']}\n")

    choice = input("Select Instance Number: ").strip()

    if not choice.isdigit():
        return None

    idx = int(choice) - 1
    if idx < 0 or idx >= len(instances):
        return None

    return instances[idx]


# ─────────────────────────────────────────────
#   SECURITY GROUP — YOUR IP ONLY
# ─────────────────────────────────────────────

def get_my_public_ip():
    """Return caller's public IP as a /32 CIDR, or None on failure."""
    import urllib.request
    try:
        ip = urllib.request.urlopen(
            "https://checkip.amazonaws.com", timeout=5
        ).read().decode().strip()
        return f"{ip}/32"
    except Exception:
        return None


def open_jenkins_port(instance_id):
    ec2 = boto3.client("ec2")
    response = ec2.describe_instances(InstanceIds=[instance_id])
    security_groups = response["Reservations"][0]["Instances"][0]["SecurityGroups"]

    my_cidr = get_my_public_ip()
    if my_cidr:
        cidr       = my_cidr
        cidr_label = f"your IP only ({my_cidr})"
    else:
        cidr       = "0.0.0.0/0"
        cidr_label = "all IPs (0.0.0.0/0) — could not detect your IP"

    for sg in security_groups:
        sg_id = sg["GroupId"]
        try:
            ec2.authorize_security_group_ingress(
                GroupId=sg_id,
                IpProtocol="tcp",
                FromPort=8080,
                ToPort=8080,
                CidrIp=cidr
            )
            print(f"[Port]  Opened 8080 on {sg_id} → {cidr_label}")
        except ClientError as e:
            if "InvalidPermission.Duplicate" in str(e):
                print(f"[Port]  8080 already open on {sg_id} — skipping")
            else:
                raise


# ─────────────────────────────────────────────
#   SSH
# ─────────────────────────────────────────────

def ssh_connect(ip, ssh_user, pem_file):
    print(f"\n[SSH]  Connecting as '{ssh_user}' to {ip}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=ip, username=ssh_user, key_filename=pem_file)
    print("[SSH]  Connected.")
    return ssh


def run_command(ssh, command, label=None):
    if label:
        print(f"       → {label}")
    else:
        print(f"\n>>> {command}")

    stdin, stdout, stderr = ssh.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()

    output = stdout.read().decode()
    error  = stderr.read().decode()

    if output:
        print(output, end="")
    if error:
        print(error, end="")

    return exit_status


# ─────────────────────────────────────────────
#   SAFE RE-RUN CHECKS
# ─────────────────────────────────────────────

def check_swap(ssh):
    """Returns True if swap is already active."""
    stdin, stdout, stderr = ssh.exec_command("swapon --show")
    return bool(stdout.read().decode().strip())


def check_java(ssh):
    """
    Returns True only if Java 21 is installed.
    java -version writes to stderr, so we combine both streams via 2>&1.
    We check for '"21.' to avoid false positives from Java 8/11/17.
    Example match: openjdk version "21.0.3" ...
    """
    stdin, stdout, stderr = ssh.exec_command("java -version 2>&1")
    output = stdout.read().decode() + stderr.read().decode()
    return "version" in output and '"21.' in output


def check_jenkins_installed(ssh):
    """Returns True if Jenkins RPM is installed."""
    stdin, stdout, stderr = ssh.exec_command("rpm -q jenkins")
    return stdout.read().decode().strip().startswith("jenkins")


def check_jenkins_running(ssh):
    """Returns True if Jenkins service is currently active."""
    stdin, stdout, stderr = ssh.exec_command(
        "sudo systemctl is-active jenkins 2>/dev/null"
    )
    return stdout.read().decode().strip() == "active"


# ─────────────────────────────────────────────
#   JENKINS ALREADY EXISTS MENU
# ─────────────────────────────────────────────

def existing_jenkins_menu(ssh, ip):
    running = check_jenkins_running(ssh)
    status  = "running" if running else "stopped"

    print(f"\n[Info]  Jenkins is already installed on this instance ({status}).\n")
    print("  What would you like to do?\n")
    print("  1. Open Jenkins in Browser")
    print("  2. Show Admin Password")
    print("  3. Start / Restart Jenkins")
    print("  4. Reinstall Jenkins")
    print("  5. Exit\n")

    choice = input("Select Option: ").strip()

    if choice == "1":
        url = f"http://{ip}:8080"
        print(f"\nOpening: {url}")
        webbrowser.open(url)

    elif choice == "2":
        password = get_jenkins_password(ssh)
        print(f"\nInitial Admin Password:\n{password}")

    elif choice == "3":
        print("\n[Service] Restarting Jenkins...")
        run_command(ssh, "sudo systemctl restart jenkins")
        print("[Service] Done.")

    elif choice == "4":
        print("\n[Warning] Reinstall will permanently delete:")
        print("          - All Jenkins jobs and pipelines")
        print("          - All credentials and plugins")
        print("          - All build history")
        print("          This cannot be undone.\n")
        if confirm("Are you sure you want to reinstall?", default="n"):
            print("\n[Reinstall] Stopping Jenkins...")
            run_command(ssh, "sudo systemctl stop jenkins || true")
            print("[Reinstall] Removing Jenkins package...")
            run_command(ssh, "sudo yum remove jenkins -y")
            print("[Reinstall] Deleting Jenkins data...")
            run_command(ssh, "sudo rm -rf /var/lib/jenkins")
            run_command(ssh, "sudo rm -f /etc/yum.repos.d/jenkins.repo")
            print("[Reinstall] Cleanup complete. Proceeding with fresh install...\n")
            return True   # signal: continue to install_jenkins()
        else:
            print("Reinstall cancelled.")

    elif choice == "5":
        print("\nExiting.")

    return False   # signal: do NOT install


# ─────────────────────────────────────────────
#   INSTALLATION
# ─────────────────────────────────────────────

def install_jenkins(ssh):
    TOTAL_STEPS = 8

    # [1/8] Update
    step(1, TOTAL_STEPS, "Updating packages")
    run_command(ssh, "sudo yum update -y")

    # [2/8] Swap
    step(2, TOTAL_STEPS, "Configuring swap memory")
    if check_swap(ssh):
        print("       Swap already configured — skipping")
    else:
        run_command(ssh, "sudo dd if=/dev/zero of=/swapfile bs=128M count=16")
        run_command(ssh, "sudo chmod 600 /swapfile")
        run_command(ssh, "sudo mkswap /swapfile")
        run_command(ssh, "sudo swapon /swapfile")
        run_command(ssh,
            "grep -q '/swapfile' /etc/fstab || "
            "echo '/swapfile swap swap defaults 0 0' | sudo tee -a /etc/fstab"
        )
        print("       Swap created (2 GB)")

    # [3/8] Java 21
    step(3, TOTAL_STEPS, "Installing Java 21 (Amazon Corretto)")
    if check_java(ssh):
        print("       Java 21 already installed — skipping")
    else:
        run_command(ssh, "sudo rpm --import https://yum.corretto.aws/corretto.key")
        run_command(ssh,
            "sudo curl -L -o /etc/yum.repos.d/corretto.repo "
            "https://yum.corretto.aws/corretto.repo"
        )
        run_command(ssh,
            "sudo yum install java-21-amazon-corretto-devel -y "
            "--enablerepo='AmazonCorretto'"
        )

    # Print installed Java version (java -version → stderr, captured via 2>&1)
    stdin, stdout, stderr = ssh.exec_command("java -version 2>&1")
    ver = stdout.read().decode().strip().split("\n")[0]
    print(f"       {ver}")

    # [4/8] Fonts
    step(4, TOTAL_STEPS, "Installing fonts (Jenkins UI fix)")
    run_command(ssh, "sudo yum install -y fontconfig dejavu-sans-fonts")
    run_command(ssh, "sudo fc-cache -fv")

    # [5/8] Jenkins repo
    step(5, TOTAL_STEPS, "Adding Jenkins repository")
    run_command(ssh,
        "sudo wget -q -O /etc/yum.repos.d/jenkins.repo "
        "https://pkg.jenkins.io/redhat-stable/jenkins.repo"
    )
    run_command(ssh,
        "sudo rpm --import https://pkg.jenkins.io/redhat-stable/jenkins.io-2023.key"
    )

    # [6/8] Install Jenkins
    step(6, TOTAL_STEPS, "Installing Jenkins")
    run_command(ssh, "sudo yum install jenkins -y")

    # [7/8] Start
    step(7, TOTAL_STEPS, "Enabling and starting Jenkins service")
    run_command(ssh, "sudo systemctl enable jenkins")
    run_command(ssh, "sudo systemctl restart jenkins")

    # [8/8] Wait for init
    step(8, TOTAL_STEPS, "Waiting for Jenkins to initialize")
    initialized = False
    for i in range(24):
        stdin, stdout, stderr = ssh.exec_command(
            "sudo test -f /var/lib/jenkins/secrets/initialAdminPassword && echo READY"
        )
        if stdout.read().decode().strip() == "READY":
            print("       Jenkins initialized successfully.")
            initialized = True
            break
        elapsed = (i + 1) * 5
        print(f"       Waiting... {elapsed}s", end="\r")
        time.sleep(5)

    if not initialized:
        print("\n[Warning] Jenkins may still be starting. Check manually.")

    print()
    run_command(ssh, "sudo systemctl status jenkins --no-pager")


# ─────────────────────────────────────────────
#   ADMIN PASSWORD
# ─────────────────────────────────────────────

def get_jenkins_password(ssh):
    stdin, stdout, stderr = ssh.exec_command(
        "sudo cat /var/lib/jenkins/secrets/initialAdminPassword 2>/dev/null"
    )
    password = stdout.read().decode().strip()
    return password if password else "Password file not found."


# ─────────────────────────────────────────────
#   MAIN
# ─────────────────────────────────────────────

def main():
    header("Jenkins Installer")

    # 1. AWS Check
    if not check_aws():
        return

    # 2. Select Instance
    print()
    selected = select_instance()
    if not selected:
        print("[Error] Invalid selection.")
        return

    print(f"\n  Name        : {selected['name']}")
    print(f"  Instance ID : {selected['id']}")
    print(f"  Public IP   : {selected['ip']}")
    print(f"  Key Pair    : {selected['key_pair']}")

    # 3. PEM File
    pem_file = select_pem_file(selected["key_pair"])
    if not pem_file:
        print("\n[Error] No valid PEM file selected. Exiting.")
        return
    print(f"[PEM]  Using: {pem_file}")

    # 4. SSH User
    ssh_user = detect_ssh_user(selected)
    print(f"[SSH]  Detected user: {ssh_user}")

    # 5. Confirm
    if not confirm("Proceed with installation?", default="y"):
        print("\nAborted.")
        return

    # 6. Open Port 8080
    print()
    open_jenkins_port(selected["id"])

    print("\n[Wait] 5 seconds before SSH...")
    time.sleep(5)

    # 7. SSH Connect
    try:
        ssh = ssh_connect(selected["ip"], ssh_user, pem_file)
    except Exception as e:
        print(f"\n[Error] SSH failed: {e}")
        return

    # 8. Jenkins already installed?
    if check_jenkins_installed(ssh):
        should_reinstall = existing_jenkins_menu(ssh, selected["ip"])
        if not should_reinstall:
            ssh.close()
            return

    # 9. Install
    print()
    install_jenkins(ssh)

    # 10. Get Password
    password = get_jenkins_password(ssh)
    ssh.close()

    # 11. Summary
    url = f"http://{selected['ip']}:8080"

    header("Jenkins Installation Complete")
    print(f"  Jenkins URL      :  {url}")
    print(f"  Admin Password   :  {password}\n")

    # 12. Open Browser
    if confirm("Open Jenkins in browser now?", default="y"):
        print(f"\n[Browser] Opening {url}")
        webbrowser.open(url)

    print("\nDone.\n")


if __name__ == "__main__":
    main()
