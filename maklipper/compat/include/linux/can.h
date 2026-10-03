/* MaKlipper compat shim: subset of Linux <linux/can.h> for macOS.
 * Klipper's chelper only uses struct can_frame framing; CAN sockets are
 * handled in Python (python-can) and are not available on macOS.
 */
#ifndef _MAKLIPPER_COMPAT_LINUX_CAN_H
#define _MAKLIPPER_COMPAT_LINUX_CAN_H

#include <stdint.h>

typedef uint32_t canid_t;

struct can_frame {
    canid_t can_id;
    union {
        uint8_t len;
        uint8_t can_dlc;
    };
    uint8_t __pad;
    uint8_t __res0;
    uint8_t len8_dlc;
    uint8_t data[8] __attribute__((aligned(8)));
};

struct canfd_frame {
    canid_t can_id;
    uint8_t len;
    uint8_t flags;
    uint8_t __res0;
    uint8_t __res1;
    uint8_t data[64] __attribute__((aligned(8)));
};

#define CAN_EFF_FLAG 0x80000000U
#define CAN_RTR_FLAG 0x40000000U
#define CAN_ERR_FLAG 0x20000000U
#define CAN_SFF_MASK 0x000007FFU
#define CAN_EFF_MASK 0x1FFFFFFFU

#endif
