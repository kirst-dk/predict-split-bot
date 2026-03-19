# -*- coding: utf-8 -*-
"""
Predict.fun - Поиск бинарных рынков
===================================

Скрипт для поиска и анализа бинарных рынков на Predict.fun
"""

import logging
from typing import List, Optional

from predict_api import PredictAPI, Market, Orderbook

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def find_binary_markets(
    api: PredictAPI,
    max_markets: int = 100,
    show_prices: bool = True,
    include_neg_risk: bool = False
) -> List[Market]:
    """
    Найти и отобразить бинарные рынки
    
    Args:
        api: API клиент
        max_markets: Максимум рынков
        show_prices: Показывать цены из ордербука
        include_neg_risk: Включить NegRisk (winner-takes-all) рынки
        
    Returns:
        Список бинарных рынков
    """
    print("\n" + "="*90)
    print("🔍 ПОИСК БИНАРНЫХ РЫНКОВ НА PREDICT.FUN")
    print("="*90)
    
    if not include_neg_risk:
        print("ℹ️  Фильтр: только обычные бинарные рынки (без NegRisk)")
        print("   NegRisk рынки не подходят для SPLIT стратегии!")
    
    print("⏳ Загрузка рынков...")
    
    markets = api.get_binary_markets(max_markets=max_markets, include_neg_risk=include_neg_risk)
    
    if not markets:
        print("❌ Бинарные рынки не найдены")
        return []
    
    print(f"✅ Найдено {len(markets)} бинарных рынков\n")
    
    # Загружаем цены
    market_data = []
    
    if show_prices:
        print("⏳ Загрузка цен...")
        for market in markets:
            try:
                ob = api.get_orderbook(market.id)
                market_data.append({
                    'market': market,
                    'orderbook': ob,
                    'yes_bid': ob.best_bid,
                    'yes_ask': ob.best_ask,
                    'spread': ob.spread,
                })
            except Exception as e:
                market_data.append({
                    'market': market,
                    'orderbook': None,
                    'yes_bid': None,
                    'yes_ask': None,
                    'spread': None,
                })
    else:
        for market in markets:
            market_data.append({
                'market': market,
                'orderbook': None,
                'yes_bid': None,
                'yes_ask': None,
                'spread': None,
            })
    
    # Выводим таблицу
    print("\n" + "-"*100)
    print(f"{'#':<4} {'ID':<8} {'Название':<35} {'Статус':<12} {'Комиссия':>10}")
    print("-"*100)
    
    for i, data in enumerate(market_data, 1):
        m = data['market']
        title = m.title[:33] + ".." if len(m.title) > 35 else m.title
        
        fee = f"{m.fee_rate_bps/100:.1f}%"
        status = m.status
        
        # Маркеры
        markers = ""
        if m.is_neg_risk:
            markers += " ⚠️NegRisk"  # NegRisk - не для SPLIT
        if data['spread'] and data['spread'] < 0.05:
            markers += " 💎"  # Низкий спред - хорошо для поинтов
        
        print(f"{i:<4} {m.id:<8} {title:<35} {status:<12} {fee:>10}{markers}")
    
    print("-"*100)
    print("💎 = Низкий спред (хорошо для PP) | 🎯 = Низкий порог акций")
    print("-"*90)
    
    return markets


def analyze_market(api: PredictAPI, market_id: int):
    """
    Детальный анализ рынка
    
    Args:
        api: API клиент
        market_id: ID рынка
    """
    print(f"\n📊 АНАЛИЗ РЫНКА #{market_id}")
    print("="*60)
    
    try:
        market = api.get_market_by_id(market_id)
        ob = api.get_orderbook(market_id)
        
        print(f"\n📋 ОСНОВНАЯ ИНФОРМАЦИЯ:")
        print(f"   ID: {market.id}")
        print(f"   Название: {market.title}")
        print(f"   Вопрос: {market.question[:80]}...")
        print(f"   Статус: {market.status}")
        print(f"   Категория: {market.category_slug}")
        print(f"   isNegRisk: {market.is_neg_risk}")
        print(f"   isYieldBearing: {market.is_yield_bearing}")
        
        print(f"\n💰 КОМИССИИ И ПОРОГИ:")
        print(f"   Комиссия: {market.fee_rate_bps} bps ({market.fee_rate_bps/100:.2f}%)")
        print(f"   Порог спреда для PP: {market.spread_threshold}%")
        print(f"   Мин. акций для PP: {market.share_threshold}")
        print(f"   Точность цены: {market.decimal_precision} знаков")
        
        print(f"\n🎯 ИСХОДЫ:")
        for outcome in market.outcomes:
            print(f"   - {outcome.name} (indexSet={outcome.index_set})")
            print(f"     Token ID: {outcome.on_chain_id[:30]}...")
        
        print(f"\n📖 ОРДЕРБУК:")
        print(f"   Лучший YES BID: {ob.best_bid:.4f}" if ob.best_bid else "   Лучший YES BID: -")
        print(f"   Лучший YES ASK: {ob.best_ask:.4f}" if ob.best_ask else "   Лучший YES ASK: -")
        print(f"   Спред: {ob.spread:.4f}" if ob.spread else "   Спред: -")
        
        no_buy, no_sell = ob.get_no_prices(market.decimal_precision)
        print(f"   NO Buy Price: {no_buy:.4f}" if no_buy else "   NO Buy Price: -")
        print(f"   NO Sell Price: {no_sell:.4f}" if no_sell else "   NO Sell Price: -")
        
        # Анализ для SPLIT стратегии
        if ob.best_ask and ob.best_bid:
            print(f"\n📈 АНАЛИЗ ДЛЯ SPLIT СТРАТЕГИИ:")
            
            # Стоимость покупки YES + NO
            yes_cost = ob.best_ask
            no_cost = no_buy
            total_cost = yes_cost + no_cost
            
            print(f"   Стоимость YES (по ASK): ${yes_cost:.4f}")
            print(f"   Стоимость NO (по NO Buy): ${no_cost:.4f}")
            print(f"   ИТОГО на $1 позиции: ${total_cost:.4f}")
            
            profit_potential = 1.0 - total_cost
            print(f"   Потенциальная прибыль: ${profit_potential:.4f} ({profit_potential*100:.2f}%)")
            
            if total_cost <= 1.0:
                print(f"   ✅ ВЫГОДНО: Покупка YES+NO <= $1")
            else:
                print(f"   ⚠️  Покупка YES+NO > $1 (убыток при исполнении)")
            
            # Рекомендуемые цены SELL
            offset = 0.01
            yes_sell = min(ob.best_ask + offset, 0.99)
            no_sell_price = min((no_sell or 0.5) + offset, 0.99)
            
            print(f"\n   Рекомендуемые SELL цены (+{offset*100:.0f}% offset):")
            print(f"   YES SELL: ${yes_sell:.4f}")
            print(f"   NO SELL: ${no_sell_price:.4f}")
            
            # Проверка порогов PP
            print(f"\n💎 ПРОВЕРКА ПОРОГОВ ДЛЯ ПОИНТОВ:")
            spread_pct = (ob.spread / ob.best_ask * 100) if ob.spread and ob.best_ask else None
            if spread_pct:
                if spread_pct <= (market.spread_threshold or 10):
                    print(f"   ✅ Спред {spread_pct:.2f}% <= порога {market.spread_threshold or 10}%")
                else:
                    print(f"   ⚠️  Спред {spread_pct:.2f}% > порога {market.spread_threshold or 10}%")
        
        print("\n" + "="*60)
        
    except Exception as e:
        print(f"❌ Ошибка анализа: {e}")


def main():
    """Главная функция"""
    print("""
    ╔════════════════════════════════════════════════════════════╗
    ║     Predict.fun - Поиск бинарных рынков                   ║
    ║     https://predict.fun                                    ║
    ╚════════════════════════════════════════════════════════════╝
    """)
    
    api = PredictAPI()
    
    # Находим рынки
    markets = find_binary_markets(api, max_markets=30, show_prices=True)
    
    if not markets:
        return
    
    # Интерактивный выбор для анализа
    while True:
        try:
            choice = input(f"\n📌 Введите ID рынка для анализа или 'q' для выхода: ").strip()
            
            if choice.lower() == 'q':
                break
            
            market_id = int(choice)
            analyze_market(api, market_id)
            
        except ValueError:
            print("❌ Введите число")
        except KeyboardInterrupt:
            break
    
    print("\n👋 До свидания!")


if __name__ == "__main__":
    main()
