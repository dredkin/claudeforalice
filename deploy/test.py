import subprocess

sudo_password = 'gezweonc'
service= 'claudeforalice'

# Тест с простой командой
result = subprocess.run(
    ["sudo", "-S", "true"],
    input=(sudo_password.strip() + "\n").encode("utf-8"),
    capture_output=True,
    timeout=10,
)
print("returncode:", result.returncode)
print("stderr:", result.stderr.decode())
