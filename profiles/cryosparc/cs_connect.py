"""Helper to get an authenticated CryoSPARC connection."""
import os
from cryosparc.tools import CryoSPARC

def get_cs():
    return CryoSPARC(
        license=os.environ["CRYOSPARC_LICENSE_ID"],
        host=os.environ["CRYOSPARC_MASTER_HOSTNAME"],
        base_port=int(os.environ["CRYOSPARC_BASE_PORT"]),
        email=os.environ["CRYOSPARC_EMAIL"],
        password=os.environ["CRYOSPARC_PASSWORD"],
    )

if __name__ == "__main__":
    cs = get_cs()
    print(cs.test_connection())
