"""BlueETL core constants."""

# Constants that can be set as environment variables to modify the behaviour of parallelization.

# If more than 10, all iterations are printed to stderr. Above 50, the output is sent to stdout.
# If not overridden, the value depends on logging: 0 if loglevel >= logging.WARNING else 10
BLUEETL_JOBLIB_VERBOSE = "BLUEETL_JOBLIB_VERBOSE"
# Number of concurrent jobs. If not overridden, it uses by default: os.cpu_count() // 2
# If 1, do not use subprocesses (mainly for testing or debugging).
BLUEETL_JOBLIB_JOBS = "BLUEETL_JOBLIB_JOBS"
# JobLib backend (loky, multiprocessing, threading). If not overridden, it uses by default: loky
BLUEETL_JOBLIB_BACKEND = "BLUEETL_JOBLIB_BACKEND"
# Logging level to be configured in subprocesses.
# If empty or not defined, use the effective log level of the parent process (recommended).
BLUEETL_SUBPROCESS_LOGGING_LEVEL = "BLUEETL_SUBPROCESS_LOGGING_LEVEL"
