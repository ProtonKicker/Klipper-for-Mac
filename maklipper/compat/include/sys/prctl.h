/* MaKlipper compat shim: macOS <sys/prctl.h>.
 * Klipper's only call is prctl(PR_SET_NAME, name) used to label threads;
 * macOS offers pthread_setname_np for the current thread. Unknown options
 * fail loudly (-1/ENOSYS) rather than pretending success.
 */
#ifndef _MAKLIPPER_COMPAT_SYS_PRCTL_H
#define _MAKLIPPER_COMPAT_SYS_PRCTL_H

#include <errno.h>
#include <pthread.h>
#include <stdarg.h>

/* Redundant where the SDK already declares it; guarantees visibility
 * regardless of feature-test macros. */
extern int pthread_setname_np(const char *name);

#define PR_SET_NAME 15

static inline int prctl(int option, ...)
{
    if (option == PR_SET_NAME) {
        va_list ap;
        const char *name;
        /* Only the first variadic arg is consumed; char* and const char*
         * share a representation (C11 6.2.5p26). */
        va_start(ap, option);
        name = va_arg(ap, const char *);
        va_end(ap);
        if (name == NULL) {
            errno = EFAULT;
            return -1;
        }
        return pthread_setname_np(name);
    }
    errno = ENOSYS;
    return -1;
}

#endif
