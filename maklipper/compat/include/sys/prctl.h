/* MaKlipper compat shim: macOS <sys/prctl.h>.
 * Klipper's only call is prctl(PR_SET_NAME, name) used to label threads.
 * macOS offers pthread_setname_np for the current thread; map onto it.
 */
#ifndef _MAKLIPPER_COMPAT_SYS_PRCTL_H
#define _MAKLIPPER_COMPAT_SYS_PRCTL_H

#include <pthread.h>
#include <stdarg.h>

#define PR_SET_NAME 15

static inline int prctl(int option, ...)
{
    if (option == PR_SET_NAME) {
        va_list ap;
        const char *name;
        va_start(ap, option);
        name = va_arg(ap, const char *);
        va_end(ap);
        return pthread_setname_np(name);
    }
    return 0;
}

#endif
