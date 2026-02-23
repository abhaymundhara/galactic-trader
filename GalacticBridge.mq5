//+------------------------------------------------------------------+
//|  GalacticBridge.mq5  — pushes every trade event to Galactic Trader|
//|  HTTP POST → http://<HOST>:<PORT>/api/mt5/trade                    |
//+------------------------------------------------------------------+
#property copyright "Galactic Trader"
#property version   "1.00"
#property strict

input string HOST     = "127.0.0.1";   // Galactic Trader host
input int    PORT     = 8080;           // Galactic Trader port
input string API_KEY  = "mt5secret";   // Must match MT5_BRIDGE_KEY in .env
input bool   LOG_ALL  = true;          // Log every push to Experts tab

// ── track last known ticket state ──────────────────────────────────
struct TicketSnap {
    ulong  ticket;
    double profit;
    string state;   // "open" | "closed"
};

TicketSnap snaps[];

// ── helpers ────────────────────────────────────────────────────────
string SideStr(ENUM_ORDER_TYPE t) {
    return (t == ORDER_TYPE_BUY  || t == DEAL_TYPE_BUY)  ? "buy"  : "sell";
}

bool HttpPost(string body) {
    char   req_body[], resp_body[];
    string resp_headers;
    StringToCharArray(body, req_body, 0, StringLen(body));
    string url     = "http://" + HOST + ":" + IntegerToString(PORT) + "/api/mt5/trade";
    string headers = "Content-Type: application/json\r\nX-API-Key: " + API_KEY + "\r\n";
    int res = WebRequest("POST", url, headers, 5000, req_body, resp_body, resp_headers);
    if (res == -1) {
        if (LOG_ALL) Print("GalacticBridge: WebRequest error ", GetLastError(),
                           " — add ", url, " to Tools > Options > Expert Advisors > allowed URLs");
        return false;
    }
    if (LOG_ALL) Print("GalacticBridge POST OK → ", CharArrayToString(resp_body));
    return true;
}

void PushTrade(string event_type,
               ulong ticket, string symbol, string side,
               double lots, double open_price, double close_price,
               double sl, double tp, double profit,
               string strategy, datetime open_time, datetime close_time) {

    string ts_open  = TimeToString(open_time,  TIME_DATE|TIME_SECONDS);
    string ts_close = TimeToString(close_time, TIME_DATE|TIME_SECONDS);

    string body = StringFormat(
        "{\"event\":\"%s\",\"ticket\":%I64u,\"symbol\":\"%s\",\"side\":\"%s\","
        "\"lots\":%.5f,\"open_price\":%.5f,\"close_price\":%.5f,"
        "\"sl\":%.5f,\"tp\":%.5f,\"profit\":%.2f,"
        "\"strategy\":\"%s\",\"open_time\":\"%s\",\"close_time\":\"%s\","
        "\"account\":\"%I64u\",\"broker\":\"%s\"}",
        event_type, ticket, symbol, side,
        lots, open_price, close_price,
        sl, tp, profit,
        strategy, ts_open, ts_close,
        AccountInfoInteger(ACCOUNT_LOGIN),
        AccountInfoString(ACCOUNT_COMPANY)
    );
    HttpPost(body);
}

// ── EA lifecycle ────────────────────────────────────────────────────
int OnInit() {
    Print("GalacticBridge initialised — pushing to ", HOST, ":", PORT);
    // seed snapshot from currently open positions
    for (int i = 0; i < PositionsTotal(); i++) {
        if (PositionSelectByTicket(PositionGetTicket(i))) {
            ulong t = PositionGetInteger(POSITION_TICKET);
            ArrayResize(snaps, ArraySize(snaps)+1);
            snaps[ArraySize(snaps)-1].ticket = t;
            snaps[ArraySize(snaps)-1].profit = PositionGetDouble(POSITION_PROFIT);
            snaps[ArraySize(snaps)-1].state  = "open";
        }
    }
    return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) {
    Print("GalacticBridge detached.");
}

// ── Main polling tick ───────────────────────────────────────────────
void OnTick() {
    // Check for newly opened positions
    for (int i = 0; i < PositionsTotal(); i++) {
        ulong ticket = PositionGetTicket(i);
        if (!PositionSelectByTicket(ticket)) continue;

        bool found = false;
        for (int j = 0; j < ArraySize(snaps); j++) {
            if (snaps[j].ticket == ticket) { found = true; break; }
        }
        if (!found) {
            // New open trade
            ArrayResize(snaps, ArraySize(snaps)+1);
            int idx = ArraySize(snaps)-1;
            snaps[idx].ticket = ticket;
            snaps[idx].profit = PositionGetDouble(POSITION_PROFIT);
            snaps[idx].state  = "open";

            PushTrade("open",
                ticket,
                PositionGetString(POSITION_SYMBOL),
                (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? "buy" : "sell",
                PositionGetDouble(POSITION_VOLUME),
                PositionGetDouble(POSITION_PRICE_OPEN), 0.0,
                PositionGetDouble(POSITION_SL),
                PositionGetDouble(POSITION_TP),
                PositionGetDouble(POSITION_PROFIT),
                PositionGetString(POSITION_COMMENT),
                (datetime)PositionGetInteger(POSITION_TIME), 0
            );
        }
    }

    // Check for closed positions via history
    HistorySelect(TimeCurrent() - 60, TimeCurrent());
    for (int i = 0; i < HistoryDealsTotal(); i++) {
        ulong deal = HistoryDealGetTicket(i);
        if (HistoryDealGetInteger(deal, DEAL_ENTRY) != DEAL_ENTRY_OUT) continue;

        ulong pos_ticket = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
        bool  already_pushed = false;
        for (int j = 0; j < ArraySize(snaps); j++) {
            if (snaps[j].ticket == pos_ticket && snaps[j].state == "closed") {
                already_pushed = true; break;
            }
        }
        if (already_pushed) continue;

        // Mark closed in snaps
        bool snap_updated = false;
        for (int j = 0; j < ArraySize(snaps); j++) {
            if (snaps[j].ticket == pos_ticket) {
                snaps[j].state  = "closed";
                snaps[j].profit = HistoryDealGetDouble(deal, DEAL_PROFIT);
                snap_updated = true; break;
            }
        }
        if (!snap_updated) {
            ArrayResize(snaps, ArraySize(snaps)+1);
            int idx = ArraySize(snaps)-1;
            snaps[idx].ticket = pos_ticket;
            snaps[idx].profit = HistoryDealGetDouble(deal, DEAL_PROFIT);
            snaps[idx].state  = "closed";
        }

        PushTrade("close",
            pos_ticket,
            HistoryDealGetString(deal, DEAL_SYMBOL),
            (HistoryDealGetInteger(deal, DEAL_TYPE) == DEAL_TYPE_SELL) ? "sell" : "buy",
            HistoryDealGetDouble(deal, DEAL_VOLUME),
            0.0,
            HistoryDealGetDouble(deal, DEAL_PRICE),
            0.0, 0.0,
            HistoryDealGetDouble(deal, DEAL_PROFIT),
            HistoryDealGetString(deal, DEAL_COMMENT),
            0,
            (datetime)HistoryDealGetInteger(deal, DEAL_TIME)
        );
    }
}
