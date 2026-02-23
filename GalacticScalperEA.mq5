//+------------------------------------------------------------------+
//|  GalacticScalperEA.mq5                                           |
//|  High-frequency multi-confirmation scalper                       |
//|  Designed for XAUUSD | M1 entries | H1 trend filter              |
//|  Target: 70 %+ win rate, 20-60 trades/day                        |
//|  Pushes every trade to Galactic Trader via GalacticBridge logic   |
//+------------------------------------------------------------------+
#property copyright "Galactic Trader"
#property version   "2.10"
#property strict

//── Inputs ────────────────────────────────────────────────────────────────────
input group "=== Risk Management ==="
input double RiskPercent       =  1.0;   // % of balance risked per trade
input double MaxDailyLossPct   =  3.0;   // Close all & stop if day-loss hits X%
input int    MaxTradesPerDay   = 50;     // Hard cap on daily trade count
input int    MaxOpenPositions  =  3;     // Concurrent open positions allowed

input group "=== Strategy Parameters ==="
input int    RSI_Period        =  7;     // RSI period (fast for scalping)
input int    BB_Period         = 20;     // Bollinger Band period
input double BB_Dev            =  2.0;   // Bollinger Band std-dev
input int    ATR_Period        = 14;     // ATR period
input double TP_ATR_Mult       =  1.2;   // TP = ATR × this
input double SL_ATR_Mult       =  1.8;   // SL = ATR × this
input double BE_Trigger_Pct    =  0.5;   // Move SL to BE when this % of TP reached
input int    Stoch_K           =  5;     // Stochastic %K
input int    Stoch_D           =  3;     // Stochastic %D
input int    Stoch_Slowing     =  3;     // Stochastic slowing
input int    EMA_Trend_Period  = 200;    // H1 EMA for trend filter
input int    EMA_Fast          = 21;     // M1 fast EMA (structure)
input int    EMA_Slow          = 50;     // M1 slow EMA (structure)

input group "=== Session Filter (GMT) ==="
input int    LondonOpen        =  7;     // London session open hour (GMT)
input int    LondonClose       = 12;     // London session close hour (GMT)
input int    NYOpen            = 13;     // New York session open hour (GMT)
input int    NYClose           = 17;     // New York session close hour (GMT)
input bool   TradeAsiaSession  = false;  // Also trade Tokyo session (02-05 GMT)

input group "=== Filters ==="
input int    MaxSpreadPoints   = 35;     // Skip if spread > X points (XAUUSD ~25 normal)
input bool   UseBreakeven      = true;   // Auto move SL to breakeven
input bool   UseTrailingStop   = true;   // ATR-based trailing stop
input double Trail_ATR_Mult    =  0.8;   // Trailing distance = ATR × this
input string EA_Comment        = "GalacticScalper"; // Order comment / strategy tag

input group "=== Galactic Bridge ==="
input string GT_HOST           = "127.0.0.1";
input int    GT_PORT           =  8080;
input string GT_API_KEY        = "mt5secret";
input bool   GT_LOG            = true;

//── Globals ───────────────────────────────────────────────────────────────────
int    h_rsi_m1, h_bb_m1, h_atr_m1, h_stoch_m1;
int    h_ema_fast_m1, h_ema_slow_m1;
int    h_ema_trend_h1;

datetime lastBarTime   = 0;
int      dailyTrades   = 0;
double   dayStartBal   = 0;
datetime dayStartDate  = 0;

//── Init ──────────────────────────────────────────────────────────────────────
int OnInit()
{
    // M1 indicators
    h_rsi_m1       = iRSI   (_Symbol, PERIOD_M1, RSI_Period, PRICE_CLOSE);
    h_bb_m1        = iBands (_Symbol, PERIOD_M1, BB_Period, 0, BB_Dev, PRICE_CLOSE);
    h_atr_m1       = iATR   (_Symbol, PERIOD_M1, ATR_Period);
    h_stoch_m1     = iStochastic(_Symbol, PERIOD_M1, Stoch_K, Stoch_D, Stoch_Slowing,
                                  MODE_SMA, STO_LOWHIGH);
    h_ema_fast_m1  = iMA    (_Symbol, PERIOD_M1, EMA_Fast, 0, MODE_EMA, PRICE_CLOSE);
    h_ema_slow_m1  = iMA    (_Symbol, PERIOD_M1, EMA_Slow, 0, MODE_EMA, PRICE_CLOSE);

    // H1 trend filter
    h_ema_trend_h1 = iMA    (_Symbol, PERIOD_H1, EMA_Trend_Period, 0, MODE_EMA, PRICE_CLOSE);

    if (h_rsi_m1 == INVALID_HANDLE || h_bb_m1 == INVALID_HANDLE ||
        h_atr_m1 == INVALID_HANDLE || h_stoch_m1 == INVALID_HANDLE ||
        h_ema_trend_h1 == INVALID_HANDLE)
    {
        Print("GalacticScalperEA: Failed to create indicator handles");
        return INIT_FAILED;
    }

    dayStartBal  = AccountInfoDouble(ACCOUNT_BALANCE);
    dayStartDate = iTime(_Symbol, PERIOD_D1, 0);

    Print("GalacticScalperEA v2.10 initialised on ", _Symbol,
          " | Risk=", RiskPercent, "% | MaxTrades=", MaxTradesPerDay);
    return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
    IndicatorRelease(h_rsi_m1);
    IndicatorRelease(h_bb_m1);
    IndicatorRelease(h_atr_m1);
    IndicatorRelease(h_stoch_m1);
    IndicatorRelease(h_ema_fast_m1);
    IndicatorRelease(h_ema_slow_m1);
    IndicatorRelease(h_ema_trend_h1);
    Print("GalacticScalperEA detached.");
}

//── Main tick ─────────────────────────────────────────────────────────────────
void OnTick()
{
    // ── Only act on new M1 bar close ─────────────────────────────────────────
    datetime barTime = iTime(_Symbol, PERIOD_M1, 0);
    if (barTime == lastBarTime) {
        // Between bars: manage open positions (breakeven, trail)
        ManagePositions();
        return;
    }
    lastBarTime = barTime;

    // ── Reset daily counters at new day ──────────────────────────────────────
    datetime todayOpen = iTime(_Symbol, PERIOD_D1, 0);
    if (todayOpen != dayStartDate) {
        dailyTrades  = 0;
        dayStartBal  = AccountInfoDouble(ACCOUNT_BALANCE);
        dayStartDate = todayOpen;
    }

    // ── Daily loss circuit breaker ────────────────────────────────────────────
    double currentBal  = AccountInfoDouble(ACCOUNT_BALANCE);
    double dailyLossPct = (dayStartBal - currentBal) / dayStartBal * 100.0;
    if (dailyLossPct >= MaxDailyLossPct) {
        if (GT_LOG) Print("GalacticScalperEA: Daily loss limit hit (",
                           DoubleToString(dailyLossPct, 2), "%) — closing all & pausing.");
        CloseAllPositions();
        return;
    }

    // ── Hard caps ─────────────────────────────────────────────────────────────
    if (dailyTrades >= MaxTradesPerDay) return;
    if (CountOpenPositions() >= MaxOpenPositions) return;

    // ── Session filter ────────────────────────────────────────────────────────
    if (!IsInSession()) return;

    // ── Spread filter ─────────────────────────────────────────────────────────
    long spreadPts = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
    if (spreadPts > MaxSpreadPoints) return;

    // ── Read indicators (bar 1 = last closed bar) ─────────────────────────────
    double rsi[3], bbUpper[2], bbMid[2], bbLower[2], atr[2];
    double stochK[3], stochD[3];
    double emaFast[2], emaSlow[2], emaTrendH1[2];
    double closeM1[3], openM1[2];

    if (!FetchBuffer(h_rsi_m1,      rsi,        3)) return;
    if (!FetchBuffer(h_bb_m1,       bbUpper,    2, UPPER_BAND))  return;
    if (!FetchBuffer(h_bb_m1,       bbMid,      2, BASE_LINE))   return;
    if (!FetchBuffer(h_bb_m1,       bbLower,    2, LOWER_BAND))  return;
    if (!FetchBuffer(h_atr_m1,      atr,        2)) return;
    if (!FetchBuffer(h_stoch_m1,    stochK,     3, 0)) return;
    if (!FetchBuffer(h_stoch_m1,    stochD,     3, 1)) return;
    if (!FetchBuffer(h_ema_fast_m1, emaFast,    2)) return;
    if (!FetchBuffer(h_ema_slow_m1, emaSlow,    2)) return;
    if (!FetchBuffer(h_ema_trend_h1,emaTrendH1, 2)) return;

    if (CopyClose(_Symbol, PERIOD_M1, 0, 3, closeM1) < 3) return;
    if (CopyOpen (_Symbol, PERIOD_M1, 1, 2, openM1)  < 2) return;

    double price   = SymbolInfoDouble(_Symbol, SYMBOL_BID);
    double atrVal  = atr[1];
    if (atrVal == 0) return;

    // ── Compute signals ───────────────────────────────────────────────────────
    bool   trendUp   = closeM1[1] > emaTrendH1[1];   // H1 EMA200 trend
    bool   trendDown = closeM1[1] < emaTrendH1[1];

    bool   structUp  = emaFast[1] > emaSlow[1];       // M1 EMA21 > EMA50
    bool   structDn  = emaFast[1] < emaSlow[1];

    // RSI crossed from oversold / overbought on closed bar
    bool rsiOversoldCross  = rsi[2] < 30.0 && rsi[1] >= 30.0;   // crossed UP through 30
    bool rsiOverboughtCross= rsi[2] > 70.0 && rsi[1] <= 70.0;   // crossed DOWN through 70

    // Stochastic K crosses D in oversold/overbought zone
    bool stochBuyCross  = stochK[2] < stochD[2] && stochK[1] > stochD[1] && stochK[1] < 40.0;
    bool stochSellCross = stochK[2] > stochD[2] && stochK[1] < stochD[1] && stochK[1] > 60.0;

    // Price near Bollinger bands (within 30% of ATR)
    bool nearLowerBB = closeM1[1] <= (bbLower[1] + atrVal * 0.3);
    bool nearUpperBB = closeM1[1] >= (bbUpper[1] - atrVal * 0.3);

    // Confirmation candle (last closed bar direction)
    bool bullCandle = closeM1[1] > openM1[1];
    bool bearCandle = closeM1[1] < openM1[1];

    // RSI not yet back in extreme zone (avoids chasing)
    bool rsiNotOverbought = rsi[1] < 65.0;
    bool rsiNotOversold   = rsi[1] > 35.0;

    // ── BUY conditions ────────────────────────────────────────────────────────
    bool buySignal = trendUp          // H1 in uptrend
                  && structUp         // M1 structure bullish
                  && (rsiOversoldCross || (rsi[1] < 35.0 && stochBuyCross))
                  && nearLowerBB      // price near / below lower BB
                  && bullCandle;      // confirmation candle

    // ── SELL conditions ───────────────────────────────────────────────────────
    bool sellSignal = trendDown
                   && structDn
                   && (rsiOverboughtCross || (rsi[1] > 65.0 && stochSellCross))
                   && nearUpperBB
                   && bearCandle;

    // ── Execute ───────────────────────────────────────────────────────────────
    if (buySignal  && !HasOpenPosition(POSITION_TYPE_BUY))  OpenTrade(ORDER_TYPE_BUY,  atrVal);
    if (sellSignal && !HasOpenPosition(POSITION_TYPE_SELL)) OpenTrade(ORDER_TYPE_SELL, atrVal);
}

//── Open a trade ──────────────────────────────────────────────────────────────
void OpenTrade(ENUM_ORDER_TYPE type, double atrVal)
{
    double ask    = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
    double bid    = SymbolInfoDouble(_Symbol, SYMBOL_BID);
    double price  = (type == ORDER_TYPE_BUY) ? ask : bid;
    double point  = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
    int    digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);

    double slDist = atrVal * SL_ATR_Mult;
    double tpDist = atrVal * TP_ATR_Mult;

    double sl, tp;
    if (type == ORDER_TYPE_BUY) {
        sl = NormalizeDouble(price - slDist, digits);
        tp = NormalizeDouble(price + tpDist, digits);
    } else {
        sl = NormalizeDouble(price + slDist, digits);
        tp = NormalizeDouble(price - tpDist, digits);
    }

    // Lot size from risk %
    double lots = CalcLots(slDist);
    if (lots <= 0) return;

    MqlTradeRequest req = {};
    MqlTradeResult  res = {};
    req.action    = TRADE_ACTION_DEAL;
    req.symbol    = _Symbol;
    req.volume    = lots;
    req.type      = type;
    req.price     = price;
    req.sl        = sl;
    req.tp        = tp;
    req.deviation = 10;
    req.magic     = 202600;
    req.comment   = EA_Comment;
    req.type_filling = ORDER_FILLING_IOC;

    if (!OrderSend(req, res)) {
        Print("GalacticScalperEA: OrderSend failed — retcode=", res.retcode,
              " desc=", res.comment);
        return;
    }
    if (res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_PLACED) {
        Print("GalacticScalperEA: Trade not filled — retcode=", res.retcode);
        return;
    }

    dailyTrades++;
    string sideStr = (type == ORDER_TYPE_BUY) ? "buy" : "sell";
    Print("GalacticScalperEA: Opened ", sideStr, " | lot=", lots,
          " | entry=", price, " | SL=", sl, " | TP=", tp,
          " | ATR=", atrVal, " | ticket=", res.order);

    // Push to Galactic Trader dashboard
    GT_PushOpen(res.order, _Symbol, sideStr, lots, price, sl, tp);
}

//── Manage open positions (breakeven + trail) ─────────────────────────────────
void ManagePositions()
{
    double atrBuf[2];
    if (!FetchBuffer(h_atr_m1, atrBuf, 2)) return;
    double atrVal = atrBuf[1];
    if (atrVal == 0) return;

    for (int i = PositionsTotal() - 1; i >= 0; i--) {
        ulong ticket = PositionGetTicket(i);
        if (!PositionSelectByTicket(ticket)) continue;
        if (PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
        if (PositionGetInteger(POSITION_MAGIC) != 202600) continue;

        double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
        double currentSL = PositionGetDouble(POSITION_SL);
        double tp        = PositionGetDouble(POSITION_TP);
        double bid       = SymbolInfoDouble(_Symbol, SYMBOL_BID);
        double ask       = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
        ENUM_POSITION_TYPE ptype = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
        int    digits    = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
        double point     = SymbolInfoDouble(_Symbol, SYMBOL_POINT);

        double tpDist    = MathAbs(tp - openPrice);
        double newSL     = currentSL;

        if (ptype == POSITION_TYPE_BUY) {
            double profit  = bid - openPrice;
            // Breakeven: move SL to entry + 1 point when halfway to TP
            if (UseBreakeven && profit >= tpDist * BE_Trigger_Pct &&
                currentSL < openPrice) {
                newSL = NormalizeDouble(openPrice + point, digits);
            }
            // Trail: move SL up behind price by Trail_ATR_Mult * ATR
            if (UseTrailingStop) {
                double trailSL = NormalizeDouble(bid - atrVal * Trail_ATR_Mult, digits);
                if (trailSL > newSL) newSL = trailSL;
            }
        } else {
            double profit  = openPrice - ask;
            if (UseBreakeven && profit >= tpDist * BE_Trigger_Pct &&
                currentSL > openPrice) {
                newSL = NormalizeDouble(openPrice - point, digits);
            }
            if (UseTrailingStop) {
                double trailSL = NormalizeDouble(ask + atrVal * Trail_ATR_Mult, digits);
                if (trailSL < newSL || currentSL == 0) newSL = trailSL;
            }
        }

        if (newSL != currentSL && newSL > 0) {
            MqlTradeRequest req = {};
            MqlTradeResult  res = {};
            req.action   = TRADE_ACTION_SLTP;
            req.symbol   = _Symbol;
            req.position = ticket;
            req.sl       = newSL;
            req.tp       = tp;
            if (!OrderSend(req, res))
                Print("GalacticScalperEA: SL/TP modify failed — ticket=", ticket,
                      " retcode=", res.retcode, " desc=", res.comment);
        }
    }
}

//── Utility: lot size based on risk% and SL distance ─────────────────────────
double CalcLots(double slDist)
{
    double balance    = AccountInfoDouble(ACCOUNT_BALANCE);
    double riskAmount = balance * RiskPercent / 100.0;
    double tickVal    = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
    double tickSize   = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
    if (tickVal == 0 || tickSize == 0 || slDist == 0) return 0;

    double lotsRaw    = riskAmount / (slDist / tickSize * tickVal);
    double lotStep    = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
    double lotMin     = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
    double lotMax     = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);

    double lots = MathFloor(lotsRaw / lotStep) * lotStep;
    lots = MathMax(lotMin, MathMin(lotMax, lots));
    return lots;
}

//── Utility: count open positions for this EA + symbol ───────────────────────
int CountOpenPositions()
{
    int count = 0;
    for (int i = 0; i < PositionsTotal(); i++) {
        if (PositionGetSymbol(i) == _Symbol &&
            PositionGetInteger(POSITION_MAGIC) == 202600) count++;
    }
    return count;
}

bool HasOpenPosition(ENUM_POSITION_TYPE ptype)
{
    for (int i = 0; i < PositionsTotal(); i++) {
        if (PositionGetSymbol(i) != _Symbol) continue;
        if (PositionGetInteger(POSITION_MAGIC) != 202600) continue;
        if ((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE) == ptype) return true;
    }
    return false;
}

void CloseAllPositions()
{
    for (int i = PositionsTotal() - 1; i >= 0; i--) {
        ulong ticket = PositionGetTicket(i);
        if (!PositionSelectByTicket(ticket)) continue;
        if (PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
        if (PositionGetInteger(POSITION_MAGIC) != 202600) continue;

        MqlTradeRequest req = {};
        MqlTradeResult  res = {};
        req.action   = TRADE_ACTION_DEAL;
        req.symbol   = _Symbol;
        req.position = ticket;
        req.volume   = PositionGetDouble(POSITION_VOLUME);
        req.type     = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY)
                       ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
        req.price    = (req.type == ORDER_TYPE_SELL)
                       ? SymbolInfoDouble(_Symbol, SYMBOL_BID)
                       : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
        req.deviation = 20;
        req.type_filling = ORDER_FILLING_IOC;
        if (!OrderSend(req, res))
            Print("GalacticScalperEA: Close failed — ticket=", ticket,
                  " retcode=", res.retcode, " desc=", res.comment);
    }
}

//── Session filter ────────────────────────────────────────────────────────────
bool IsInSession()
{
    MqlDateTime t;
    TimeGMT(t);
    int h = t.hour;

    bool london = (h >= LondonOpen  && h < LondonClose);
    bool ny     = (h >= NYOpen      && h < NYClose);
    bool asia   = TradeAsiaSession && (h >= 2 && h < 5);
    return london || ny || asia;
}

//── Buffer helper ─────────────────────────────────────────────────────────────
bool FetchBuffer(int handle, double &buf[], int count, int bufIdx = 0)
{
    ArraySetAsSeries(buf, true);
    if (CopyBuffer(handle, bufIdx, 0, count, buf) < count) return false;
    return true;
}

//── Galactic Trader bridge push (open event only) ────────────────────────────
void GT_PushOpen(ulong ticket, string symbol, string side,
                 double lots, double price, double sl, double tp)
{
    char   req_body[], resp_body[];
    string resp_headers;
    string ts = TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS);
    string body = StringFormat(
        "{\"event\":\"open\",\"ticket\":%I64u,\"symbol\":\"%s\",\"side\":\"%s\","
        "\"lots\":%.5f,\"open_price\":%.5f,\"close_price\":0,"
        "\"sl\":%.5f,\"tp\":%.5f,\"profit\":0,"
        "\"strategy\":\"%s\",\"open_time\":\"%s\",\"close_time\":\"\","
        "\"account\":\"%I64u\",\"broker\":\"%s\"}",
        ticket, symbol, side, lots, price, sl, tp,
        EA_Comment, ts,
        AccountInfoInteger(ACCOUNT_LOGIN),
        AccountInfoString(ACCOUNT_COMPANY)
    );
    StringToCharArray(body, req_body, 0, StringLen(body));
    string url     = "http://" + GT_HOST + ":" + IntegerToString(GT_PORT) + "/api/mt5/trade";
    string headers = "Content-Type: application/json\r\nX-API-Key: " + GT_API_KEY + "\r\n";
    int res = WebRequest("POST", url, headers, 5000, req_body, resp_body, resp_headers);
    if (GT_LOG) {
        if (res == -1)
            Print("GT push failed — add ", url, " to Expert Advisors allowed URLs");
        else
            Print("GT push OK → ", CharArrayToString(resp_body));
    }
}
