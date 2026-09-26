import storage
import board
import displayio

# Clear display immediately so boot text never shows
board.DISPLAY.root_group = displayio.Group()

# Make the filesystem writable so the dashboard can persist cached usage state.
storage.remount("/", readonly=False)
