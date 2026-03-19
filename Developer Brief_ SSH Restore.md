# **Feature Update: Post-Reboot SSH Backup Restore for Vœrynth Sentinel**

**Hi \[Developer's Name\],**

We are adding a "Fail-Forward" recovery tier to our ha\_watchdog.py script.

Currently, if the ThinkCentre NUC suffers a hard freeze, the Pi 5 power cycles the Tuya plug and waits for the BOOT\_GRACE\_PERIOD. However, if the HAOS database or configuration is corrupted, the system will just boot back into a broken state, and the watchdog will eventually loop.

I want to add an automated SSH fallback. I have already successfully tested the physical infrastructure and SSH key authentication from the Pi 5 to the NUC.

### **1\. Infrastructure Status (Already Completed)**

* The **Terminal & SSH** add-on is installed on HAOS, with Protection Mode **OFF**.  
* The Pi 5’s ed25519 public key is added to the authorized\_keys.  
* **Note on Auth:** The SSH user is ha (not root), and the command requires a login shell (bash \-l \-c) to load the SUPERVISOR\_TOKEN automatically.  
* **Note on Backups:** Our backups are encrypted, so we must pass the \--password flag in the CLI command.

### **2\. Dependency Update**

Please update deploy\_to\_pi.sh to include paramiko in the pip install string:

pip3 install \--break-system-packages requests tinytuya paramiko

### **3\. Adjust Timeout Thresholds (Crucial for Updates)**

To ensure the watchdog doesn't accidentally pull the plug during a legitimate Host OS reboot or a lengthy Core update, please update these existing configuration variables at the top of ha\_watchdog.py:

\# \--- UPDATE THESE EXISTING CONFIG VARIABLES \---

\# Allows \~3 minutes (18 checks \* 10s) of total silence to account for intentional Host OS reboots/updates  
HARD\_FAILURE\_THRESHOLD \= 18   

\# Allows 10 minutes (600s) for massive Core/Supervisor updates where the Observer is alive but Core is installing  
SOFT\_FAILURE\_TIMEOUT \= 600    

### **4\. The Python Implementation**

Please add this SSH execution function to ha\_watchdog.py. The repository version must keep hostnames, keys, and passwords outside source control and read them from environment variables or `config.env`.

import paramiko

def trigger\_ssh\_backup\_restore():  
    """  
    Connects to the HA Server via SSH and triggers a restore   
    of the preferred backup selected at runtime.  
    """  
    BACKUP\_PASS \= os.getenv("BACKUP_PASS")  
    HA\_HOST \= os.getenv("HA_HOST", "homeassistant.local")  
      
    ssh \= paramiko.SSHClient()  
    ssh.set\_missing\_host\_key\_policy(paramiko.AutoAddPolicy())  
      
    logger.info("Initiating SSH connection to trigger HA backup restore...")  
    try:  
        \# Note: Username must be 'ha' for the Home Assistant add-on  
        ssh.connect(HA\_HOST, port=22, username='ha', timeout=10)  
          
        \# Wrapped in a login shell to get the SUPERVISOR\_TOKEN, with password decryption  
        restore\_cmd \= f"bash \-l \-c \\"ha backups restore \\\\$(ha backups list \--raw-json | jq \-r '.data.backups\[0\].slug') \--password '{BACKUP\_PASS}'\\""  
          
        stdin, stdout, stderr \= ssh.exec\_command(restore\_cmd)  
        exit\_status \= stdout.channel.recv\_exit\_status()  
          
        if exit\_status \== 0:  
            logger.warning("SSH Backup Restore command executed successfully.")  
            return True  
        else:  
            logger.error(f"SSH Backup Restore failed with exit status {exit\_status}. Error: {stderr.read().decode().strip()}")  
            return False  
              
    except Exception as e:  
        logger.error(f"Failed to connect via SSH or execute restore. OS might be completely dead: {e}")  
        return False  
    finally:  
        ssh.close()

### **5\. Logic Integration**

To ensure we **do not** trigger a restore during normal HA restarts or OS updates, this logic must **only** fire after a power cycle has already been executed.

**Context for the Flow:** The script already takes into account the 120-second (now 600s) soft failure timeout. If only the Core is off for that duration, it properly escalates to a power cycle. **Therefore, the SSH restore should strictly act as a "last resort" *after* that power cycle, the boot grace period, and the cooldown period have passed, and the system is *still* inactive.**

Please inject this logic immediately *after* the BOOT\_GRACE\_PERIOD sleep that follows a power\_cycle\_host() event.

**Execution Flow:**

1. Soft Failure Timeout triggers normally \-\> Tuya Power Cycle.  
2. Wait out BOOT\_GRACE\_PERIOD (and any related cooldowns).  
3. **\[NEW\]** Run an immediate health check (check\_ha\_health()).  
4. **\[NEW\]** If the Core is *still* unreachable after the reboot cycle, it means the OS booted but the HA Application is corrupted. Execute trigger\_ssh\_backup\_restore().  
5. **\[NEW\]** Sleep for an extended recovery window (e.g., 400 seconds) to allow the backup to extract and the NUC to rebuild itself before the watchdog resumes its normal polling loop.

Here is the pseudo-integration for the main loop:

\# ... inside the existing power cycle execution block ...  
success \= power\_cycle\_host()  
if success:  
    logger.warning(f"Power cycle done. Waiting {BOOT\_GRACE\_PERIOD}s before re-checking...")  
    time.sleep(BOOT\_GRACE\_PERIOD)  
      
    \# \--- NEW POST-BOOT VERIFICATION & RESTORE LOGIC \---  
    logger.info("Boot grace period ended. Verifying HA Core status...")  
    core\_ok, \_ \= check\_ha\_health()  
      
    if not core\_ok:  
        logger.error("HA Core is STILL offline after a hard reboot. Suspecting database/config corruption.")  
        restore\_success \= trigger\_ssh\_backup\_restore()  
          
        if restore\_success:  
            logger.warning("Backup restore initiated. Pausing monitoring for 400s to allow extraction and rebuild...")  
            time.sleep(400) \# Give HA time to rebuild from the heavy backup  
              
            \# Reset counters to give the fresh system a clean slate  
            hard\_failures \= 0  
            soft\_failures \= 0  
            last\_reboot\_ts \= time.time() \# Reset cooldown timer  
    else:  
        logger.info("HA Core recovered successfully post-reboot. No restore needed.")  
    \# \--------------------------------------------------

Please review and integrate this into the state machine. Let me know when the updated scripts are ready for staging\!