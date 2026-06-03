CALL    = "Call"
PUT     = "Put"
CONFIRM = "confirm"
CANCEL  = "cancel"

# positions
POS_CLOSE_PREFIX = "pclose:"   # pclose:0, pclose:1, ...
POS_BACK         = "pos_back"

# pending orders
ORD_SELECT_PREFIX = "osel:"    # osel:0, osel:1, ...
ORD_CANCEL        = "ocancel"
ORD_MODIFY        = "omodify"
ORD_BACK          = "oback"

# signal confirmation (distinct from CONFIRM/CANCEL to avoid collision)
SIG_CONFIRM      = "sig_confirm"
SIG_CANCEL       = "sig_cancel"
SIG_CHANGE_PRICE = "sig_chprice"
SIG_EDIT         = "sig_edit"

# change price on any order confirmation
CHANGE_PRICE = "change_price"

# login flow
LOGIN_LIVE  = "login_live"
LOGIN_PAPER = "login_paper"
