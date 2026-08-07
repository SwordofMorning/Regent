##
 # @file mk/lib/paths.py
 # @date 2026/08/07
 # 
 # @brief Runtime path resolution helpers for Dandelion (frozen/dev aware).
 #
 # @note Path priority for base_dir():
 #   1. $DANDELION_HOME (explicit override)
 #   2. Executable directory (frozen/Nuitka standalone build)
 #   3. Repository root (source run)
 #

import os
import sys

##
 # @brief Detect frozen (compiled) runtime.
 #
 # @return True when running inside a Nuitka standalone binary.
 #
def is_compiled():
    return "__compiled__" in globals()
# End-def

##
 # @brief Resolve the runtime base directory.
 #
 # @return Absolute base directory path (str).
 #
def base_dir():
    env_home = os.environ.get("DANDELION_HOME")
    if env_home:
        return env_home
    # End-if

    if is_compiled():
        # Layout: <root>/bin/dandelion.exe
        # Return parent of bin/ to reach the actual workspace root
        return os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))
    # End-if

    # Source run: <root>/mk/lib/paths.py -> <root>/mk/lib -> <root>/mk -> <root>
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# End-def

##
 # @brief Resolve the config directory (.env).
 #
 # @return Absolute config directory path (str).
 #
def config_dir():
    return os.path.join(base_dir(), ".env")
# End-def

##
 # @brief Resolve the log directory (.log).
 #
 # @return Absolute log directory path (str).
 #
def log_dir():
    return os.path.join(base_dir(), ".log")
# End-def

##
 # @brief Resolve the api config file path.
 #
 # @return Absolute path to api.cfg (str).
 #
def config_path():
    return os.path.join(config_dir(), "api.cfg")
# End-def
