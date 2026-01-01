import docker

client = docker.from_env()

try:
    container = client.containers.get("auto-debian11")
    
    if container.status == "running":
        # Check the file content
        exit_code, output = container.exec_run(
            "cat /etc/apt/sources.list.d/pgedge.list",
            user="root"
        )
        
        if exit_code == 0:
            content = output.decode().strip()
            print(f"Current content of /etc/apt/sources.list.d/pgedge.list:")
            print(content)
            print()
            
            if "staging" in content:
                print("✅ File contains 'staging'")
            else:
                print("❌ File does NOT contain 'staging'")
                
            if "release" in content:
                print("⚠️ File still contains 'release'")
            else:
                print("✅ File does NOT contain 'release'")
        else:
            print(f"Failed to read file: {output.decode()}")
    else:
        print(f"Container is not running (status: {container.status})")
        
except docker.errors.NotFound:
    print("Container auto-debian11 not found")
except Exception as e:
    print(f"Error: {e}")
