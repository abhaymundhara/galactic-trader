//+------------------------------------------------------------------+
//|                                       SimpleTrendScalper_ATR.mq5 |
//| M15 trend scalper with % risk, ATR SL/TP, BE, trailing & timeout |
//|  DEMO USE ONLY                                                   |
//+------------------------------------------------------------------+
#property strict

//--- Inputs: money management
input double RiskPerTradePercent  = 0.5;   // % equity risked per trade
input int    MaxOpenPositions     = 30;    // max open trades by this EA
input int    MaxTradesPerDay      = 50000; // cap so it doesn't go crazy
input double MinLots              = 0.01;  // user lot min
input double MaxLots              = 1.00;  // user lot max
input double MaxDailyLossPercent  = 50.0;   // stop opening new trades after this DD (% of day start equity)

//--- Inputs: trade parameters
input ENUM_TIMEFRAMES TradeTF     = PERIOD_M15; // trading timeframe (M15 recommended)
input int    FastMAPeriod         = 10;
input int    SlowMAPeriod         = 30;

//--- ATR-based dynamic SL/TP
input int    ATRPeriod            = 14;    // ATR period on TradeTF
input double SL_ATR_Multiplier    = 2.0;   // SL distance = ATR * this
input double TP_ATR_Multiplier    = 3.0;   // TP distance = ATR * this

//--- Trade management (break-even & trailing) in *points*
input double BE_TriggerPoints     = 150.0; // move SL to BE after this profit
input double TrailStartPoints     = 200.0; // start trailing after this profit
input double TrailStepPoints      = 50.0;  // trail distance in points

//--- Time stop: close trade after N bars if neither SL nor TP hit
input int    MaxBarsInTrade       = 20;    // e.g. 20 M15 bars = 300 minutes

//--- Optional session filter (server time)
input bool   UseSessionFilter     = false; // if true, only trade between hours below
input int    SessionStartHour     = 7;     // e.g. 7 = 07:00
input int    SessionEndHour       = 20;    // e.g. 20 = 20:00 (8 PM)

//--- Magic number
input long   MagicNumber          = 112233;

//--- indicator handles
int fastMAHandle = INVALID_HANDLE;
int slowMAHandle = INVALID_HANDLE;
int atrHandle    = INVALID_HANDLE;

//--- daily tracking
datetime tradingDayDate = 0;
double   dayStartEquity = 0.0;
int      tradesToday    = 0;

//+------------------------------------------------------------------+
//| Return just the date (midnight) for a datetime                   |
//+------------------------------------------------------------------+
datetime DateOfDay(datetime t)
{
   MqlDateTime st;
   TimeToStruct(t, st);
   st.hour = 0;
   st.min  = 0;
   st.sec  = 0;
   return StructToTime(st);
}

//+------------------------------------------------------------------+
//| Check if time is within session                                  |
//+------------------------------------------------------------------+
bool IsWithinSession(datetime t)
{
   if(!UseSessionFilter)
      return true;

   MqlDateTime st;
   TimeToStruct(t, st);
   int hour = st.hour;

   if(SessionStartHour <= SessionEndHour)
   {
      // normal case, e.g. 7 -> 20
      return (hour >= SessionStartHour && hour < SessionEndHour);
   }
   else
   {
      // overnight window, e.g. 22 -> 5
      return (hour >= SessionStartHour || hour < SessionEndHour);
   }
}

//+------------------------------------------------------------------+
//| OnInit                                                           |
//+------------------------------------------------------------------+
int OnInit()
{
   fastMAHandle = iMA(_Symbol, TradeTF, FastMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   slowMAHandle = iMA(_Symbol, TradeTF, SlowMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   atrHandle    = iATR(_Symbol, TradeTF, ATRPeriod);

   if(fastMAHandle == INVALID_HANDLE ||
      slowMAHandle == INVALID_HANDLE ||
      atrHandle    == INVALID_HANDLE)
   {
      Print("Failed to create indicator handles");
      return(INIT_FAILED);
   }

   datetime now = TimeCurrent();
   tradingDayDate = DateOfDay(now);
   dayStartEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   tradesToday    = 0;

   Print("SimpleTrendScalper_ATR initialized on ", _Symbol,
         " TF=", EnumToString(TradeTF),
         " dayStartEquity=", DoubleToString(dayStartEquity,2));
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| OnDeinit                                                         |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(fastMAHandle != INVALID_HANDLE) IndicatorRelease(fastMAHandle);
   if(slowMAHandle != INVALID_HANDLE) IndicatorRelease(slowMAHandle);
   if(atrHandle    != INVALID_HANDLE) IndicatorRelease(atrHandle);
}

//+------------------------------------------------------------------+
//| Count open positions for this EA and symbol                      |
//+------------------------------------------------------------------+
int CountOpenPositions()
{
   int total = PositionsTotal();
   int count = 0;

   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket)) continue;

      long   magic = (long)PositionGetInteger(POSITION_MAGIC);
      string sym   = (string)PositionGetString(POSITION_SYMBOL);

      if(magic == MagicNumber && sym == _Symbol)
         count++;
   }
   return count;
}

//+------------------------------------------------------------------+
//| Calculate lot size from % risk and SL distance                   |
//+------------------------------------------------------------------+
double CalculateLotSize(double slDistance)
{
   if(slDistance <= 0)
      return 0.0;

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(equity <= 0)
      return 0.0;

   double riskAmount = equity * (RiskPerTradePercent / 100.0);

   double tickValue = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double point     = _Point;

   if(tickValue <= 0 || tickSize <= 0 || point <= 0)
      return 0.0;

   double valuePerPointPerLot = tickValue / tickSize * point;
   double slPoints            = slDistance / point;
   if(slPoints <= 0)
      return 0.0;

   double riskPerLot = slPoints * valuePerPointPerLot; // currency risk for 1 lot
   if(riskPerLot <= 0)
      return 0.0;

   double lots = riskAmount / riskPerLot;

   // symbol volume limits
   double minLotStep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double minLotSym  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLotSym  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);

   double minAllowed = MinLots;
   double maxAllowed = MaxLots;

   if(minLotSym > 0)
      minAllowed = MathMax(minAllowed, minLotSym);
   if(maxLotSym > 0)
      maxAllowed = MathMin(maxAllowed, maxLotSym);

   lots = MathMax(lots, minAllowed);
   lots = MathMin(lots, maxAllowed);

   if(minLotStep > 0)
      lots = MathRound(lots / minLotStep) * minLotStep;

   lots = NormalizeDouble(lots, 2);

   Print("Calculated lots=", DoubleToString(lots,2),
         " risk=", DoubleToString(RiskPerTradePercent,2), "%, slDistance=",
         DoubleToString(slDistance/_Point,1)," pts");
   return lots;
}

//+------------------------------------------------------------------+
//| Open a trade                                                     |
//+------------------------------------------------------------------+
bool OpenTrade(ENUM_ORDER_TYPE type, double lots, double slDistance, double tpDistance)
{
   MqlTradeRequest req;
   MqlTradeResult  res;
   ZeroMemory(req);
   ZeroMemory(res);

   double price;
   if(type == ORDER_TYPE_BUY)
      price = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   else
      price = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   if(price <= 0)
      return false;

   double sl, tp;
   if(type == ORDER_TYPE_BUY)
   {
      sl = price - slDistance;
      tp = price + tpDistance;
   }
   else
   {
      sl = price + slDistance;
      tp = price - tpDistance;
   }

   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = _Symbol;
   req.type         = type;
   req.volume       = lots;
   req.price        = price;
   req.sl           = NormalizeDouble(sl, _Digits);
   req.tp           = NormalizeDouble(tp, _Digits);
   req.deviation    = 30;
   req.magic        = MagicNumber;
   req.type_filling = ORDER_FILLING_FOK;

   if(!OrderSend(req, res))
   {
      Print("OrderSend failed. Error=", GetLastError());
      return false;
   }

   if(res.retcode != TRADE_RETCODE_DONE &&
      res.retcode != TRADE_RETCODE_PLACED)
   {
      Print("OrderSend retcode=", res.retcode);
      return false;
   }

   Print("Opened ", EnumToString(type),
         " lots=", DoubleToString(lots,2),
         " price=", DoubleToString(price,_Digits),
         " SL=", DoubleToString(sl,_Digits),
         " TP=", DoubleToString(tp,_Digits),
         " ticket=", res.order);
   return true;
}

//+------------------------------------------------------------------+
//| Get trend direction from fast/slow EMA                           |
//+------------------------------------------------------------------+
int GetTrendDirection()
{
   double fastBuf[2], slowBuf[2];
   if(CopyBuffer(fastMAHandle, 0, 0, 2, fastBuf) != 2) return 0;
   if(CopyBuffer(slowMAHandle, 0, 0, 2, slowBuf) != 2) return 0;

   double fast = fastBuf[0];
   double slow = slowBuf[0];

   // small no-trade band to avoid chop
   if(MathAbs(fast - slow) < (2 * _Point))
      return 0;

   if(fast > slow) return 1;   // uptrend
   if(fast < slow) return -1;  // downtrend
   return 0;
}

//+------------------------------------------------------------------+
//| Manage open positions: BE, trailing, time-stop                   |
//+------------------------------------------------------------------+
void ManageOpenPositions()
{
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(ticket)) continue;

      long   magic = (long)PositionGetInteger(POSITION_MAGIC);
      string sym   = (string)PositionGetString(POSITION_SYMBOL);

      if(magic != MagicNumber || sym != _Symbol)
         continue;

      int    type       = (int)PositionGetInteger(POSITION_TYPE);
      double priceOpen  = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl         = PositionGetDouble(POSITION_SL);
      double tp         = PositionGetDouble(POSITION_TP);
      double volume     = PositionGetDouble(POSITION_VOLUME);
      datetime timeOpen = (datetime)PositionGetInteger(POSITION_TIME);

      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      if(bid <= 0 || ask <= 0)
         continue;

      double currentPrice = (type == POSITION_TYPE_BUY ? bid : ask);
      double profitPoints = (currentPrice - priceOpen) / _Point;
      if(type == POSITION_TYPE_SELL)
         profitPoints = (priceOpen - currentPrice) / _Point;

      //--- time stop: close trade after MaxBarsInTrade bars
      if(MaxBarsInTrade > 0)
      {
         int tfSeconds = PeriodSeconds(TradeTF);
         if(tfSeconds > 0)
         {
            int barsHeld = (int)((TimeCurrent() - timeOpen) / tfSeconds);
            if(barsHeld >= MaxBarsInTrade)
            {
               MqlTradeRequest cReq;
               MqlTradeResult  cRes;
               ZeroMemory(cReq);
               ZeroMemory(cRes);

               cReq.action   = TRADE_ACTION_DEAL;
               cReq.symbol   = _Symbol;
               cReq.position = ticket;
               cReq.volume   = volume;
               cReq.type     = (type == POSITION_TYPE_BUY ? ORDER_TYPE_SELL : ORDER_TYPE_BUY);
               cReq.price    = (type == POSITION_TYPE_BUY ? bid : ask);
               cReq.deviation= 30;
               cReq.magic    = MagicNumber;

               if(!OrderSend(cReq, cRes))
               {
                  Print("Failed to close timed-out position ticket=", ticket,
                        " err=", GetLastError());
               }
               else
               {
                  Print("Closed timed-out position ticket=", ticket,
                        " after barsHeld=", barsHeld);
               }
               continue; // move to next position
            }
         }
      }

      double newSL = sl;

      //--- move to break-even
      if(BE_TriggerPoints > 0 && profitPoints >= BE_TriggerPoints)
      {
         double bePrice = priceOpen;
         if(type == POSITION_TYPE_BUY)
         {
            if(sl < bePrice)
               newSL = bePrice;
         }
         else // SELL
         {
            if(sl > bePrice || sl == 0.0)
               newSL = bePrice;
         }
      }

      //--- trailing stop
      if(TrailStartPoints > 0 && TrailStepPoints > 0 && profitPoints >= TrailStartPoints)
      {
         double trailPrice;
         if(type == POSITION_TYPE_BUY)
            trailPrice = currentPrice - TrailStepPoints * _Point;
         else
            trailPrice = currentPrice + TrailStepPoints * _Point;

         if(type == POSITION_TYPE_BUY)
         {
            if(trailPrice > newSL)
               newSL = trailPrice;
         }
         else // SELL
         {
            if(newSL == 0.0 || trailPrice < newSL)
               newSL = trailPrice;
         }
      }

      //--- apply SL change if needed
      if(newSL != sl && newSL > 0.0)
      {
         MqlTradeRequest req;
         MqlTradeResult  res;
         ZeroMemory(req);
         ZeroMemory(res);

         req.action   = TRADE_ACTION_SLTP;
         req.symbol   = _Symbol;
         req.position = ticket;
         req.sl       = NormalizeDouble(newSL, _Digits);
         req.tp       = tp;

         if(!OrderSend(req, res))
         {
            Print("Modify SL failed. Ticket=", ticket,
                  " Error=", GetLastError());
         }
      }
   }
}

//+------------------------------------------------------------------+
//| OnTick: manage trades, risk check, then one trade per new bar    |
//+------------------------------------------------------------------+
void OnTick()
{
   datetime now    = TimeCurrent();
   double   equity = AccountInfoDouble(ACCOUNT_EQUITY);

   //--- new day reset
   datetime today = DateOfDay(now);
   if(today != tradingDayDate)
   {
      tradingDayDate = today;
      dayStartEquity = equity;
      tradesToday    = 0;
      Print("New day, reset tradesToday. Equity=", DoubleToString(equity,2));
   }

   //--- manage existing trades (BE, trailing, time-stop)
   ManageOpenPositions();

   //--- daily loss guard (only count drawdown, ignore profit; reset if weird)
   if(MaxDailyLossPercent > 0.0)
   {
      // safety: if start equity is invalid or equity collapsed/changed massively (e.g. new demo),
      // re-anchor the dayStartEquity to current equity
      if(dayStartEquity <= 0.0 || equity <= 0.0)
      {
         dayStartEquity = equity;
         tradesToday    = 0;
         Print("Daily-loss guard: resetting dayStartEquity to ", DoubleToString(equity,2), " (invalid previous value)");
      }

      double ddPct = 0.0;
      if(equity < dayStartEquity && dayStartEquity > 0.0)
      {
         ddPct = (dayStartEquity - equity) / dayStartEquity * 100.0;
      }
      else
      {
         // in profit or flat -> no drawdown
         ddPct = 0.0;
      }

      PrintFormat("DD check: dayStartEquity=%.2f, equity=%.2f, ddPct=%.2f",
                  dayStartEquity, equity, ddPct);

      if(ddPct >= MaxDailyLossPercent)
      {
         Print("MaxDailyLossPercent reached: ", DoubleToString(ddPct,2),
               "%. No new trades today.");
         return;
      }
   }

   //--- trades per day cap
   if(tradesToday >= MaxTradesPerDay)
      return;

   //--- optional session filter
   if(!IsWithinSession(now))
      return;

   //--- detect new bar on TradeTF
   datetime barTime = iTime(_Symbol, TradeTF, 0);
   static datetime lastBarTime = 0;

   if(barTime == 0)
      return;
   if(barTime == lastBarTime)
      return; // same bar, do nothing

   lastBarTime = barTime;

   //--- position limit
   int openCount = CountOpenPositions();
   if(openCount >= MaxOpenPositions)
      return;

   //--- get trend direction
   int dir = GetTrendDirection(); // 1=BUY, -1=SELL, 0=none
   if(dir == 0)
      return;

   //--- ATR-based SL/TP distances
   double atrBuf[1];
   if(CopyBuffer(atrHandle, 0, 0, 1, atrBuf) != 1)
   {
      Print("Failed to get ATR");
      return;
   }
   double atr = atrBuf[0];
   if(atr <= 0)
      return;

   double slDistance = atr * SL_ATR_Multiplier;
   double tpDistance = atr * TP_ATR_Multiplier;
   if(slDistance <= 0 || tpDistance <= 0)
      return;

   //--- dynamic lot size
   double lots = CalculateLotSize(slDistance);
   if(lots <= 0)
      return;

   bool ok = false;
   if(dir == 1)
      ok = OpenTrade(ORDER_TYPE_BUY, lots, slDistance, tpDistance);
   else if(dir == -1)
      ok = OpenTrade(ORDER_TYPE_SELL, lots, slDistance, tpDistance);

   if(ok)
      tradesToday++;
}
//+------------------------------------------------------------------+